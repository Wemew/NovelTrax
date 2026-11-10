"""qBittorrent adapter implementing the ``DownloaderClient`` protocol.

This is a thin wrapper: the client keeps doing the actual work (qBittorrent downloads
straight into the configured save path), so existing behaviour is untouched.  The
adapter exists so the download orchestration/UI can treat engines uniformly and so the
Torbox engine has a reference implementation of the contract.
"""

import logging
from typing import Any, Dict, List, Optional

from app.downloader.base import DownloaderClient, DownloaderError, extract_torrent_hash
from app.downloader.qbit import QBitClient

logger = logging.getLogger("noveltrax.downloader.qbit_adapter")

_QBIT_READY_STATES = {"uploading", "stalledUP", "pausedUP", "queuedUP", "checkingUP", "forcedUP"}


class QBitDownloader:
    """Protocol adapter over :class:`QBitClient` (zero behaviour change)."""

    name = "qbit"

    def __init__(self, client: QBitClient):
        self.client = client

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.client, "enabled", False))

    def test_connection(self) -> Dict[str, Any]:
        return self.client.test_connection()

    def submit(self, release: Any, savepath: str, wanted_volumes: Optional[List[float]] = None) -> Dict[str, Any]:
        url = _release_attr(release, "download_url") or _release_attr(release, "link") or ""
        success = self.client.add_torrent(
            url_or_magnet=url,
            savepath=savepath,
            category=self.client.category,
            tags=self.client.tags,
        )
        if not success:
            raise DownloaderError("qBittorrent rejected the torrent (see downloader status).")
        handle = {"hash": extract_torrent_hash(url) or "", "engine": self.name}
        return {"handle": handle, "state": "downloading", "cached": False}

    def status(self, handle: Dict[str, Any]) -> Dict[str, Any]:
        torrent_hash = (handle or {}).get("hash") or ""
        files = []
        state = "queued"
        progress = 0.0
        try:
            torrents = self.client.get_torrents() or []
        except Exception:
            torrents = []
        match = next((t for t in torrents if str(t.get("hash") or "") == torrent_hash), None)
        if match:
            raw_state = str(match.get("state") or "")
            progress = float(match.get("progress") or 0.0)
            if raw_state in _QBIT_READY_STATES:
                state = "ready"
            elif raw_state.lower() in ("stalleddl", "metadl", "checkingdl", "pauseddl"):
                state = "stalled" if raw_state.lower() == "stalleddl" else "queued"
            elif raw_state.lower() in ("error", "missingfiles"):
                state = "failed"
            else:
                state = "downloading"
            files = [{"id": idx, "name": f.get("name"), "size": f.get("size")}
                     for idx, f in enumerate(self.client.get_torrent_files(torrent_hash) or [])]
        return {"state": state, "progress": progress, "files": files, "handle": dict(handle or {})}

    def fetch_files(self, handle: Dict[str, Any], files: Any, dest_dir: str, progress_cb=None) -> List[Dict[str, Any]]:
        # qBittorrent writes files into the save path itself; nothing to fetch.
        return []

    def cancel(self, handle: Dict[str, Any], delete_remote: bool = True) -> bool:
        torrent_hash = (handle or {}).get("hash") or ""
        if not torrent_hash:
            return False
        return bool(self.client.delete_torrent(torrent_hash, delete_files=not delete_remote))


def _release_attr(release: Any, name: str) -> Any:
    if isinstance(release, dict):
        return release.get(name)
    return getattr(release, name, None)
