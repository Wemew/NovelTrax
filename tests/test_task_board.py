"""Persisted background-task store/serializer tests (live status board)."""

import asyncio
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_BASE = os.path.dirname(_HERE)
for _p in (_BASE, os.path.join(_BASE, "backend"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app import background_tasks  # noqa: E402
from app.database import Database  # noqa: E402
from app.scheduler import TaskManager  # noqa: E402
from app.task_projection import sync_download_tasks  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def test_lifecycle_and_persistence():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    db.init_db()
    task_id = db.create_background_task("feed_scan", "Feed scan", scope="all", actor="test")
    check(task_id > 0, "task id")

    queued = db.get_background_task(task_id)
    check(queued["status"] == "queued", queued)
    check(background_tasks.serialize_task(queued)["active"] is True, queued)
    db.start_background_task(task_id)
    running = db.get_background_task(task_id)
    check(running["status"] == "running", running)
    db.update_background_task(task_id, done=2, total=4, message="Scanning feed 2/4")
    running = db.get_background_task(task_id)
    payload = background_tasks.serialize_task(running)
    check(payload["active"] is True, payload)
    check(payload["progress_pct"] == 50.0, payload)
    check(payload["remaining"] == 2, payload)
    check(payload["message"] == "Scanning feed 2/4", payload)
    check(payload["cancellable"] is False, payload)

    db.finish_background_task(task_id, "failed", "boom")
    failed = db.get_background_task(task_id)
    check(failed["status"] == "failed", failed)
    check(failed["error"] == "boom", failed)
    check(failed["progress_pct"] == 50.0, failed)

    reopened = Database(path)
    reopened.init_db()
    persisted = reopened.get_background_task(task_id)
    check(persisted is not None and persisted["error"] == "boom", persisted)
    check(reopened.list_background_tasks(active_only=True) == [], "no active")


def test_stale_tasks_marked_failed():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    db.init_db()
    queued = db.create_background_task("feed_scan", "Queued")
    running = db.create_background_task("backfill", "Running")
    db.start_background_task(running)
    db.finish_background_task(db.create_background_task("calibre_sync", "Done"), "success")
    check(db.mark_stale_background_tasks() == 2, "two stale rows")
    check(db.get_background_task(queued)["status"] == "failed", "queued stale")
    check(db.get_background_task(running)["status"] == "failed", "running stale")


def test_stalled_task_signal():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    db.init_db()
    task_id = db.create_background_task("backfill", "Slow backfill", status="running")
    with db.get_connection() as conn:
        conn.execute("UPDATE background_tasks SET updated_at = datetime('now', '-20 minutes') WHERE id = ?;", (task_id,))
        conn.commit()
    payload = background_tasks.serialize_task(db.get_background_task(task_id))
    check(payload["stalled"] is True, payload)
    check(payload["seconds_since_update"] >= 1190, payload)
    db.update_background_task(task_id, message="heartbeat")
    payload = background_tasks.serialize_task(db.get_background_task(task_id))
    check(payload["stalled"] is False, payload)


def test_success_marks_complete():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    db.init_db()
    task_id = db.create_background_task("calibre_sync", "Calibre sync")
    db.finish_background_task(task_id, "success", result={"series": 3})
    task = db.get_background_task(task_id)
    payload = background_tasks.serialize_task(task)
    check(payload["status"] == "success", payload)
    check(payload["progress_pct"] == 100.0, payload)
    check(payload["result"] == {"series": 3}, payload)
    check(payload["active"] is False, payload)


def test_decorator_lifecycle():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    db.init_db()

    class Dummy:
        def __init__(self):
            self.db = db
            self._task_reporter = None
        def _task_report(self, **kwargs):
            if self._task_reporter:
                self._task_reporter.report(**kwargs)
        @background_tasks.tracked_task("dummy", "Dummy task")
        def work(self):
            self._task_report(total=2, done=1, message="halfway")
            return {"ok": 1}
        @background_tasks.tracked_task("dummy_fail", "Dummy failure")
        def fail(self):
            raise ValueError("nope")

    dummy = Dummy()
    check(dummy.work() == {"ok": 1}, "decorator result")
    tasks = db.list_background_tasks(limit=10)
    ok_task = next(t for t in tasks if t["kind"] == "dummy")
    check(ok_task["status"] == "success", ok_task)
    check(ok_task["progress_pct"] == 100.0, ok_task)
    try:
        dummy.fail()
    except ValueError:
        pass
    failed = next(t for t in db.list_background_tasks(limit=10) if t["kind"] == "dummy_fail")
    check(failed["status"] == "failed" and "nope" in failed["error"], failed)


def test_download_tasks_are_persisted_once():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    db.init_db()
    _, release_id = db.upsert_release({
        "guid": "transfer-task-1",
        "title": "Transfer Task Volume 1 Epub",
        "link": "https://nyaa.si/view/1",
        "series_name": "Transfer Task",
        "volume": 1.0,
    })
    db.update_downloader_state(release_id, "DOWNLOADING", "torbox")
    sync_download_tasks(db)
    tasks = [t for t in db.list_background_tasks(limit=20) if t["kind"] == "download"]
    check(len(tasks) == 1, tasks)
    first_id = tasks[0]["id"]
    check(db.get_background_task(first_id)["status"] == "running", tasks[0])

    sync_download_tasks(db)
    tasks = [t for t in db.list_background_tasks(limit=20) if t["kind"] == "download"]
    check(len(tasks) == 1 and tasks[0]["id"] == first_id, tasks)

    db.update_downloader_state(release_id, "COMPLETED", "torbox")
    sync_download_tasks(db)
    finished = db.get_background_task(first_id)
    check(finished["status"] == "success", finished)
    check(finished["progress_pct"] == 100.0, finished)


def test_taskmanager_decorators_are_wired():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    db.init_db()

    class FakeManager:
        def __init__(self):
            self.db = db
            self._task_reporter = None
            self.is_checking_feeds = True
            self.is_syncing_calibre = True
            self.is_backfilling = True
        def _task_report(self, **kwargs):
            pass

    fake = FakeManager()
    TaskManager.check_feeds(fake)
    TaskManager.sync_calibre(fake)
    asyncio.run(TaskManager.run_polite_backfill(fake))
    kinds = {row["kind"] for row in db.list_background_tasks(limit=10)}
    check("feed_scan" in kinds, kinds)
    check("calibre_sync" in kinds, kinds)
    check("backfill" in kinds, kinds)
    check(all(row["status"] == "success" for row in db.list_background_tasks(limit=10)), db.list_background_tasks(limit=10))


def test_global_board_markup():
    base = open(os.path.join(_BASE, "backend", "app", "templates", "base.html"), encoding="utf-8").read()
    for needle in ("tasks-panel", "tasks-toggle", "tasks-active-count", "NovelTasks", "setInterval(poll, 1000)", "/api/tasks?active=0&limit=20"):
        check(needle in base, f"base.html missing {needle}")
    check("status === 'success' ? 'completed'" in base, "completed label")


def main():
    test_lifecycle_and_persistence()
    test_stale_tasks_marked_failed()
    test_stalled_task_signal()
    test_success_marks_complete()
    test_decorator_lifecycle()
    test_download_tasks_are_persisted_once()
    test_taskmanager_decorators_are_wired()
    test_global_board_markup()
    print("task board suite PASSED")


if __name__ == "__main__":
    main()
