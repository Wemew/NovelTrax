"""Downloader engine abstraction (TorBox port).

NovelTrax can drive more than one download engine.  This module defines the small
protocol both engines implement plus the shared helpers (typed errors and torrent
hash extraction).  ``qbit.py`` remains the qBittorrent client; ``qbit_adapter.py``
adapts it behind this protocol with zero behaviour change, and ``torbox.py`` adds the
Torbox debrid engine using only the standard library.

Engine selection lives in ``downloader/__init__.py`` (setting ``downloader_engine``).
"""

import base64
import logging
import re
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

logger = logging.getLogger("noveltrax.downloader")


class DownloaderError(Exception):
    """Base class for actionable downloader failures (messages never contain secrets)."""


class DownloaderAuthError(DownloaderError):
    """Authentication/authorisation failure (bad or missing API key)."""


class DownloaderRateLimitError(DownloaderError):
    """HTTP 429 from the engine; ``retry_after`` is the advertised delay in seconds."""

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class DownloaderSlotError(DownloaderError):
    """The engine refused the job because its active-slot/concurrency limit is full."""


class DownloaderNotFoundError(DownloaderError):
    """The requested job/torrent does not exist (anymore)."""


class DownloaderFileError(DownloaderError):
    """A downloaded file was missing, unsafe or failed validation."""


# Torbox can queue a submission instead of activating it when the account has no free
# active download slot.  The persisted ``downloader_id`` encodes that distinction so
# the poller knows whether to poll ``mylist`` (id) or ``getqueued`` (queued: id).
QUEUED_ID_PREFIX = "queued:"


def encode_queued_handle_id(queued_id: Any) -> str:
    """Encode a Torbox queued id for ``releases.downloader_id``."""
    return f"{QUEUED_ID_PREFIX}{int(queued_id)}"


def parse_queued_handle_id(value: Any) -> Optional[int]:
    """Return the queued id encoded in ``downloader_id`` (None for active ids)."""
    text = str(value or "").strip()
    if not text.lower().startswith(QUEUED_ID_PREFIX):
        return None
    try:
        return int(text[len(QUEUED_ID_PREFIX):].strip())
    except (TypeError, ValueError):
        return None


def extract_torrent_hash(url_or_magnet: str) -> Optional[str]:
    """Return the normalised 40-char hex infohash from a magnet/string, if present.

    Shared by both engines (previously only ``QBitClient.extract_torrent_hash``).
    Supports 40-character hex and 32-character base32 btih values.
    """
    if not url_or_magnet or not isinstance(url_or_magnet, str):
        return None
    match = re.search(r"urn:btih:([a-zA-Z0-9]{32,40})", url_or_magnet, re.IGNORECASE)
    if not match:
        return None
    raw_hash = match.group(1).lower()
    if len(raw_hash) == 32:
        try:
            return base64.b32decode(raw_hash.upper()).hex().lower()
        except Exception:
            return raw_hash
    return raw_hash


@runtime_checkable
class DownloaderClient(Protocol):
    """Minimal engine contract used by the scheduler and the status API."""

    name: str
    enabled: bool

    def test_connection(self) -> Dict[str, Any]:
        """Return ``{success, message, ...}`` without raising for connectivity errors."""

    def submit(self, release: Any, savepath: str, wanted_volumes: Optional[List[float]] = None) -> Dict[str, Any]:
        """Queue *release* for download; returns a handle dict persisted on the release."""

    def status(self, handle: Dict[str, Any]) -> Dict[str, Any]:
        """Return ``{state, progress, files, ...}`` with state in queued|downloading|stalled|ready|failed."""

    def fetch_files(self, handle: Dict[str, Any], files: Any, dest_dir: str, progress_cb=None) -> List[Dict[str, Any]]:
        """Fetch the selected files into *dest_dir*; returns one dict per saved file."""

    def cancel(self, handle: Dict[str, Any], delete_remote: bool = True) -> bool:
        """Cancel/delete the engine-side job."""
