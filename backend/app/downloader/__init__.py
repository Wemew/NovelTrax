"""
NovelTrax Downloader Package.
Provides qBittorrent client integration and selective volume prioritization.
"""

from app.downloader.qbit import (
    QBitClient,
    ConnectionTestResult,
    SelectiveDownloadResult,
    parse_torrent_filename_volumes,
)

__all__ = [
    "QBitClient",
    "ConnectionTestResult",
    "SelectiveDownloadResult",
    "parse_torrent_filename_volumes",
]
