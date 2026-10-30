import logging
import httpx
from typing import Dict, Any, Optional

logger = logging.getLogger("noveltrax.notifier")

class NotificationService:
    def __init__(self, discord_webhook: Optional[str] = None, generic_webhook: Optional[str] = None):
        self.discord_webhook = discord_webhook
        self.generic_webhook = generic_webhook

    def notify_new_request(self, release: Dict[str, Any]):
        """Trigger notification when a new book is requested."""
        title = release.get("title", "")
        series = release.get("series_name", "Unknown Series")
        vol = release.get("volume", "")
        link = release.get("link", "")
        cover = release.get("cover_url")

        content = f"📖 **New Novel Release Requested!**\n**Series:** {series}\n**Volume:** {vol}\n**Title:** {title}\n🔗 [View Release]({link})"

        self._send_discord(
            title=f"New Release: {series} Vol {vol}",
            description=f"**{title}** has been added to your requested list.",
            url=link,
            image_url=cover,
            color=0x3498db # Blue
        )
        self._send_generic({"event": "NEW_REQUEST", "release": release})

    def notify_acquired(self, release: Dict[str, Any]):
        """Trigger notification when a requested book is added to Calibre."""
        title = release.get("title", "")
        series = release.get("series_name", "Unknown Series")
        vol = release.get("volume", "")

        self._send_discord(
            title=f"✅ Added to Calibre: {series} Vol {vol}",
            description=f"**{title}** was detected in your Calibre library and resolved!",
            color=0x2ecc71 # Green
        )
        self._send_generic({"event": "ACQUIRED", "release": release})

    def _send_discord(self, title: str, description: str, url: Optional[str] = None, image_url: Optional[str] = None, color: int = 0x3498db):
        if not self.discord_webhook:
            return

        payload = {
            "username": "NovelTrax",
            "avatar_url": "https://raw.githubusercontent.com/feathericons/feather/master/icons/book-open.svg",
            "embeds": [{
                "title": title,
                "description": description,
                "url": url,
                "color": color,
                "thumbnail": {"url": image_url} if image_url else None
            }]
        }

        try:
            with httpx.Client(timeout=5.0) as client:
                client.post(self.discord_webhook, json=payload)
        except Exception as e:
            logger.error(f"Failed to deliver Discord notification: {e}")

    def _send_generic(self, payload: Dict[str, Any]):
        if not self.generic_webhook:
            return

        try:
            with httpx.Client(timeout=5.0) as client:
                client.post(self.generic_webhook, json=payload)
        except Exception as e:
            logger.error(f"Failed to deliver generic webhook notification: {e}")
