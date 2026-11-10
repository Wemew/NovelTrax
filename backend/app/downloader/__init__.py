"""
NovelTrax Downloader Package.
Provides qBittorrent client integration and selective volume prioritization, plus the
download-engine abstraction used by the TorBox debrid engine.
"""

import logging
import os
from typing import Any, Optional

from app.downloader.base import DownloaderClient, DownloaderError
from app.downloader.qbit import (
    QBitClient,
    ConnectionTestResult,
    SelectiveDownloadResult,
    parse_torrent_filename_volumes,
)
from app.downloader.qbit_adapter import QBitDownloader
from app.downloader.torbox import TorboxDownloader

logger = logging.getLogger("noveltrax.downloader")

DOWNLOADER_ENGINES = ("qbit", "torbox")
DEFAULT_DOWNLOADER_ENGINE = "qbit"

__all__ = [
    "QBitClient",
    "ConnectionTestResult",
    "SelectiveDownloadResult",
    "parse_torrent_filename_volumes",
    "DownloaderClient",
    "DownloaderError",
    "QBitDownloader",
    "TorboxDownloader",
    "DOWNLOADER_ENGINES",
    "DEFAULT_DOWNLOADER_ENGINE",
    "as_bool",
    "resolve_downloader_engine",
    "build_downloader",
]


def as_bool(value: Any, default: bool = False) -> bool:
    """Truthy setting parser (``"true"``/``"1"``/``"yes"``/``"on"``)."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    if text in ("1", "true", "yes", "on", "enabled"):
        return True
    if text in ("0", "false", "no", "off", "disabled"):
        return False
    return default


def resolve_downloader_engine(db) -> str:
    """Return the configured engine (``qbit`` default; unknown values fall back)."""
    raw = (db.get_setting("downloader_engine", DEFAULT_DOWNLOADER_ENGINE) or "").strip().lower()
    if raw in DOWNLOADER_ENGINES:
        return raw
    if raw:
        logger.warning("Unknown downloader_engine %r; using %s.", raw, DEFAULT_DOWNLOADER_ENGINE)
    return DEFAULT_DOWNLOADER_ENGINE


def build_downloader(db, engine: Optional[str] = None, qbit_client=None) -> DownloaderClient:
    """Construct the configured engine (Torbox needs no qBittorrent client)."""
    selected = (engine or resolve_downloader_engine(db)).strip().lower()
    if selected == "torbox":
        try:
            max_download_gb = float(db.get_setting("torbox_max_download_gb", "64") or 64)
        except (TypeError, ValueError):
            max_download_gb = 64.0
        max_download_gb = max(1.0, min(max_download_gb, 10240.0))
        try:
            download_concurrency = int(db.get_setting("torbox_download_concurrency", "4") or 4)
        except (TypeError, ValueError):
            download_concurrency = 4
        download_concurrency = max(1, min(download_concurrency, 16))
        try:
            parallel_chunks = int(db.get_setting("torbox_parallel_chunks", "4") or 4)
        except (TypeError, ValueError):
            parallel_chunks = 4
        parallel_chunks = max(1, min(parallel_chunks, 8))
        return TorboxDownloader(
            api_key=db.get_setting("torbox_api_key", "") or "",
            enabled=as_bool(db.get_setting("torbox_enabled", "false"), default=False),
            delete_after_download=as_bool(
                db.get_setting("torbox_delete_after_download", "true"), default=True,
            ),
            max_file_bytes=int(max_download_gb * 1024 * 1024 * 1024),
            max_concurrency=download_concurrency,
            parallel_chunks=parallel_chunks,
        )
    if qbit_client is None:
        qbit_client = _build_qbit_client(db)
    return QBitDownloader(qbit_client)


def _build_qbit_client(db) -> QBitClient:
    """Build a :class:`QBitClient` from the ``qbit_*`` settings (``qbit_url`` wins)."""
    url = str(db.get_setting("qbit_url", "") or "").strip()
    host = str(db.get_setting("qbit_host", "") or "").strip()
    port_str = str(db.get_setting("qbit_port", "") or "").strip()
    use_ssl_text = str(db.get_setting("qbit_use_ssl", "false") or "false").lower()

    if url and not host:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 8080)
        use_ssl = parsed.scheme == "https"
    else:
        host = host or "localhost"
        port = int(port_str) if port_str.isdigit() else 8080
        use_ssl = use_ssl_text in ("true", "1", "yes")

    return QBitClient(
        host=host,
        port=port,
        username=db.get_setting("qbit_username", "admin"),
        password=db.get_setting("qbit_password", ""),
        use_ssl=use_ssl,
        base_url=url if url else None,
        enabled=as_bool(db.get_setting("qbit_enabled", "true"), default=True),
        category=db.get_setting("qbit_category", "novels") or "novels",
        tags=db.get_setting("qbit_tags", "noveltrax") or "noveltrax",
        savepath=db.get_setting("qbit_savepath", os.getenv("DOWNLOAD_DIR", "/downloads")),
    )
