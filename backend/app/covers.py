import os
import hashlib
import logging
import socket
import ipaddress
from typing import Optional, Tuple
from urllib.parse import urlparse, urljoin
try:
    import httpx
except ImportError:
    httpx = None

logger = logging.getLogger("noveltrax.covers")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/jpeg,image/png,*/*;q=0.8",
}

# Security limits
MAX_COVER_BYTES = 5 * 1024 * 1024  # 5 MB maximum image size
DOWNLOAD_TIMEOUT_SECONDS = 7.0
MAX_REDIRECTS = 3

def is_safe_public_url(url: str) -> bool:
    """
    Prevents Server-Side Request Forgery (SSRF).
    Ensures that the URL targets a public HTTP/HTTPS host,
    and explicitly blocks private, loopback, link-local, and reserved IP ranges.
    """
    if not url or not isinstance(url, str):
        return False

    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False

        hostname = parsed.hostname
        if not hostname:
            return False

        # Disallow localhost by name
        if hostname.lower() in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            return False

        # Resolve DNS addresses
        addr_info = socket.getaddrinfo(hostname, None)
        for family, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            ip = ipaddress.ip_address(ip_str)

            # Block private, loopback, cloud metadata (169.254.x.x), multicast, or reserved IPs
            if (ip.is_private or ip.is_loopback or ip.is_link_local 
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                logger.warning(f"SSRF Protection blocked request to private/internal IP: {ip_str} ({url})")
                return False

        return True
    except Exception as e:
        logger.debug(f"SSRF check failed/blocked for URL {url}: {e}")
        return False

def detect_valid_image(data: bytes) -> Optional[str]:
    """
    Verifies image authenticity by inspecting file magic bytes.
    Guarantees that files downloaded to disk are legitimate raster images
    and never malicious HTML, executables, or script-bearing SVGs.
    """
    if len(data) < 16:
        return None

    # JPEG: FF D8 FF
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"

    # PNG: 89 50 4E 47 0D 0A 1A 0A
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"

    # WEBP: RIFF .... WEBP
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"

    # GIF: GIF87a or GIF89a
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"

    return None

class CoverCache:
    def __init__(self, data_dir: str):
        self.covers_dir = os.path.join(data_dir, "covers")
        os.makedirs(self.covers_dir, exist_ok=True)

    def get_filename(self, url: str) -> str:
        """Derive a safe, unguessable filename based on SHA-256 of the URL."""
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        return f"{url_hash}.jpg"

    def get_path(self, url: str) -> str:
        return os.path.join(self.covers_dir, self.get_filename(url))

    def is_cached(self, url: str) -> bool:
        if not url:
            return False
        path = self.get_path(url)
        return os.path.isfile(path) and os.path.getsize(path) > 0

    async def get_or_download(self, url: str) -> Optional[str]:
        """
        Safely fetches and caches a cover image.
        Validates initial URL and all subsequent redirect hops against SSRF,
        and validates magic bytes before writing to disk.
        """
        if not url:
            return None

        filepath = self.get_path(url)
        if os.path.isfile(filepath) and os.path.getsize(filepath) > 0:
            return filepath

        temp_path = f"{filepath}.tmp"
        current_url = url

        try:
            async with httpx.AsyncClient(headers=HEADERS, timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=False) as client:
                for redirect_idx in range(MAX_REDIRECTS + 1):
                    if not is_safe_public_url(current_url):
                        logger.warning(f"Blocked unsafe/internal cover URL or redirect target: {current_url}")
                        return None

                    async with client.stream("GET", current_url) as resp:
                        # Handle redirects manually to validate every hop against SSRF
                        if resp.status_code in (301, 302, 303, 307, 308):
                            loc = resp.headers.get("location")
                            if not loc:
                                return None
                            current_url = urljoin(current_url, loc)
                            continue

                        if resp.status_code != 200:
                            logger.warning(f"Failed to fetch image from {current_url}: HTTP {resp.status_code}")
                            return None

                        content_length = resp.headers.get("content-length")
                        if content_length and int(content_length) > MAX_COVER_BYTES:
                            logger.warning(f"Image too large ({content_length} bytes) for {current_url}")
                            return None

                        header_chunk = b""
                        downloaded_bytes = 0

                        with open(temp_path, "wb") as f:
                            async for chunk in resp.aiter_bytes(chunk_size=8192):
                                downloaded_bytes += len(chunk)
                                if downloaded_bytes > MAX_COVER_BYTES:
                                    logger.warning(f"Image exceeded max byte limit during stream: {current_url}")
                                    f.close()
                                    if os.path.exists(temp_path):
                                        os.remove(temp_path)
                                    return None

                                if len(header_chunk) < 32:
                                    header_chunk += chunk

                                f.write(chunk)

                        # Validate magic bytes
                        image_type = detect_valid_image(header_chunk)
                        if not image_type:
                            logger.warning(f"Rejecting non-image or unsafe file format downloaded from {current_url}")
                            if os.path.exists(temp_path):
                                os.remove(temp_path)
                            return None

                        # Atomic move into final cache location
                        if os.path.exists(filepath):
                            os.remove(filepath)
                        os.rename(temp_path, filepath)
                        logger.debug(f"Successfully cached {image_type} cover from {current_url}")
                        return filepath

                logger.warning(f"Too many redirects ({MAX_REDIRECTS}) fetching cover: {url}")
                return None

        except Exception as e:
            logger.debug(f"Cover download error for {url}: {e}")
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            return None

    def cache_sync(self, url: str) -> Optional[str]:
        """Synchronous version for background tasks with multi-hop SSRF and magic byte checks."""
        if not url:
            return None

        filepath = self.get_path(url)
        if os.path.isfile(filepath) and os.path.getsize(filepath) > 0:
            return filepath

        temp_path = f"{filepath}.tmp"
        current_url = url

        try:
            with httpx.Client(headers=HEADERS, timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=False) as client:
                for redirect_idx in range(MAX_REDIRECTS + 1):
                    if not is_safe_public_url(current_url):
                        return None

                    with client.stream("GET", current_url) as resp:
                        if resp.status_code in (301, 302, 303, 307, 308):
                            loc = resp.headers.get("location")
                            if not loc:
                                return None
                            current_url = urljoin(current_url, loc)
                            continue

                        if resp.status_code != 200:
                            return None

                        content_length = resp.headers.get("content-length")
                        if content_length and int(content_length) > MAX_COVER_BYTES:
                            return None

                        header_chunk = b""
                        downloaded_bytes = 0

                        with open(temp_path, "wb") as f:
                            for chunk in resp.iter_bytes(chunk_size=8192):
                                downloaded_bytes += len(chunk)
                                if downloaded_bytes > MAX_COVER_BYTES:
                                    f.close()
                                    if os.path.exists(temp_path):
                                        os.remove(temp_path)
                                    return None

                                if len(header_chunk) < 32:
                                    header_chunk += chunk

                                f.write(chunk)

                        image_type = detect_valid_image(header_chunk)
                        if not image_type:
                            if os.path.exists(temp_path):
                                os.remove(temp_path)
                            return None

                        if os.path.exists(filepath):
                            os.remove(filepath)
                        os.rename(temp_path, filepath)
                        return filepath

                return None

        except Exception as e:
            logger.debug(f"Sync cover cache error: {e}")
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            return None
