"""Project release-level transfer state into persisted background-task rows."""

from __future__ import annotations

from typing import Any, Dict

from app.download_state import release_download_state


def sync_download_tasks(db: Any) -> None:
    """Create/update one persisted task per active transfer and close finished ones."""
    active = db.list_active_downloads(50)
    active_ids = set()
    for release in active:
        release_id = int(release.get("id") or 0)
        if not release_id:
            continue
        active_ids.add(str(release_id))
        state = release_download_state(release)
        task = db.find_background_task_by_scope("download", str(release_id))
        if not task or task.get("status") not in ("queued", "running"):
            task_id = db.create_background_task(
                "download", f"Transfer: {release.get('title') or release_id}",
                scope=str(release_id), status="running",
            )
        else:
            task_id = int(task["id"])
        progress = state.get("progress_pct") or 0.0
        db.update_background_task(
            task_id,
            total=1 if progress > 0 else 0,
            done=1 if progress > 0 else 0,
            progress_pct=progress,
            message=f"{state.get('label')} · {state.get('engine') or 'downloader'}",
        )
    for task in db.list_background_tasks(active_only=True, kind="download", limit=200):
        scope = str(task.get("scope") or "")
        if scope in active_ids:
            continue
        try:
            release = db.get_release_by_id(int(scope))
        except (TypeError, ValueError):
            release = None
        if release and release.get("download_status") == "COMPLETED":
            db.finish_background_task(int(task["id"]), "success")
        else:
            message = (release or {}).get("download_error") or "Transfer is no longer active"
            db.finish_background_task(int(task["id"]), "failed", message)
