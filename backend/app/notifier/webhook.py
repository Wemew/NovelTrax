import logging
import json
import urllib.request
import urllib.error
from typing import Dict, Any, Optional

try:
    import httpx
except ImportError:
    httpx = None

logger = logging.getLogger("noveltrax.notifier")

class SimpleHttpResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text

def _post_json(url: str, json_data: dict, timeout: float = 10.0) -> SimpleHttpResponse:
    if httpx:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=json_data)
            return SimpleHttpResponse(resp.status_code, resp.text)
    else:
        req = urllib.request.Request(
            url,
            data=json.dumps(json_data).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "NovelTrax/1.0"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status if hasattr(resp, "status") else resp.getcode()
                text = resp.read().decode("utf-8", errors="ignore")
                return SimpleHttpResponse(status, text)
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="ignore")
            return SimpleHttpResponse(e.code, text)

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
            _post_json(self.discord_webhook, payload, timeout=5.0)
        except Exception as e:
            logger.error(f"Failed to deliver Discord notification: {e}")

    def _send_generic(self, payload: Dict[str, Any]):
        if not self.generic_webhook:
            return

        try:
            _post_json(self.generic_webhook, payload, timeout=5.0)
        except Exception as e:
            logger.error(f"Failed to deliver generic webhook notification: {e}")

    def test_webhook(self, discord_url: Optional[str] = None, apprise_url: Optional[str] = None) -> Dict[str, Any]:
        """
        Sends a test notification to verify Discord and/or generic webhook endpoints.
        Returns detailed status and response messages for user diagnostics.
        """
        d_url = discord_url if discord_url is not None else self.discord_webhook
        g_url = apprise_url if apprise_url is not None else self.generic_webhook

        if not d_url and not g_url:
            return {
                "status": "error",
                "message": "No webhook URL provided. Please enter a Discord Webhook URL or Generic Webhook URL."
            }

        results = {}
        all_succeeded = True

        if d_url:
            payload = {
                "username": "NovelTrax",
                "avatar_url": "https://raw.githubusercontent.com/feathericons/feather/master/icons/book-open.svg",
                "embeds": [{
                    "title": "🔔 NovelTrax Notification Test",
                    "description": "If you're reading this, your Discord webhook integration is functioning properly!",
                    "color": 0x6366f1,
                    "fields": [
                        {"name": "Status", "value": "✅ Connected", "inline": True},
                        {"name": "Application", "value": "NovelTrax", "inline": True}
                    ]
                }]
            }
            try:
                resp = _post_json(d_url.strip(), payload, timeout=10.0)
                if resp.status_code in (200, 204):
                    results["discord"] = {
                        "success": True,
                        "message": f"Discord notification delivered successfully (HTTP {resp.status_code})!"
                    }
                else:
                    all_succeeded = False
                    results["discord"] = {
                        "success": False,
                        "message": f"Discord returned HTTP {resp.status_code}: {resp.text[:200]}"
                    }
            except Exception as e:
                all_succeeded = False
                results["discord"] = {
                    "success": False,
                    "message": f"Connection error: {str(e)}"
                }

        if g_url:
            payload = {
                "event": "TEST",
                "title": "NovelTrax Notification Test",
                "message": "Your generic/Apprise webhook endpoint is reachable and configured properly!"
            }
            try:
                resp = _post_json(g_url.strip(), payload, timeout=10.0)
                if 200 <= resp.status_code < 300:
                    results["generic"] = {
                        "success": True,
                        "message": f"Generic webhook delivered successfully (HTTP {resp.status_code})!"
                    }
                else:
                    all_succeeded = False
                    results["generic"] = {
                        "success": False,
                        "message": f"Generic webhook returned HTTP {resp.status_code}: {resp.text[:200]}"
                    }
            except Exception as e:
                all_succeeded = False
                results["generic"] = {
                    "success": False,
                    "message": f"Connection error: {str(e)}"
                }

        messages = [r["message"] for r in results.values()]
        return {
            "status": "success" if all_succeeded else "error",
            "message": " | ".join(messages),
            "results": results
        }
