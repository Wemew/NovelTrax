"""Reconcile completed qBittorrent torrents into release status."""

from __future__ import annotations

from typing import Any, Dict, Iterable

COMPLETE_STATES = frozenset({
    "uploading", "stalledup", "pausedup", "queuedup", "forcedup", "completed",
})


def sync_qbit_completed(db: Any, torrents: Iterable[Dict[str, Any]], on_completed=None) -> int:
    """Mark matching releases COMPLETED/DOWNLOADED for finished torrents."""
    updated = 0
    for torrent in torrents or []:
        if not isinstance(torrent, dict):
            continue
        torrent_hash = str(torrent.get("hash") or "").strip().lower()
        if not torrent_hash:
            continue
        try:
            progress = float(torrent.get("progress") or 0)
        except (TypeError, ValueError):
            progress = 0.0
        state = str(torrent.get("state") or "").strip().lower()
        if progress < 0.999 and state not in COMPLETE_STATES:
            continue
        release = db.get_release_by_torrent_hash(torrent_hash)
        if not release:
            continue
        release_id = int(release.get("id") or 0)
        if not release_id:
            continue
        newly_completed = release.get("download_status") != "COMPLETED"
        if newly_completed:
            db.update_downloader_state(release_id, "COMPLETED", "qbit")
        db.mark_release_downloaded(release_id)
        if newly_completed and on_completed is not None:
            try:
                on_completed(release, torrent)
            except Exception:
                pass
        updated += 1
    return updated
