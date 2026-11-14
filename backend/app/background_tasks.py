"""Persisted background-task runner with thread-safe, coalesced progress.

Usage::

    task = run_in_background(db, "sync:calibre", "Sync Calibre", worker, actor="user")
    # worker(report): report(done=1, total=10, message="Scanning…")

*state* lives in SQLite (``background_tasks``), so the UI can reload the page and
reattach to a running task via ``GET /api/tasks?active=1``.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("noveltrax.tasks")

MAX_WORKERS = 3
_WRITE_INTERVAL_SECONDS = 0.5
_PCT_EPSILON = 1.0
_EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="noveltrax-task")
_CANCEL_FLAGS: Dict[int, threading.Event] = {}
_RUNNING: Dict[int, str] = {}
_LOCK = threading.Lock()


def _elapsed_seconds(task: Dict[str, Any]) -> float:
    started = task.get("started_at")
    if not started:
        return 0.0
    try:
        start = datetime.strptime(str(started), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return 0.0
    end = datetime.utcnow()
    finished = task.get("finished_at")
    if finished:
        try:
            end = datetime.strptime(str(finished), "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            pass
    return max(0.0, (end - start).total_seconds())


def serialize_task(task: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not task:
        return None
    payload = dict(task)
    payload["elapsed_seconds"] = round(_elapsed_seconds(task), 1)
    try:
        payload["progress_pct"] = round(float(task.get("progress_pct") or 0.0), 1)
    except (TypeError, ValueError):
        payload["progress_pct"] = 0.0
    payload["active"] = payload.get("status") in ("queued", "running")
    payload["cancellable"] = False
    payload["result"] = {}
    if task.get("result_json"):
        try:
            payload["result"] = json.loads(task["result_json"])
        except (TypeError, ValueError):
            payload["result"] = {}
    total = int(task.get("items_total") or 0)
    done = int(task.get("items_done") or 0)
    remaining = max(0, total - done) if total else 0
    payload["remaining"] = remaining
    elapsed = payload["elapsed_seconds"]
    if total and done and elapsed:
        payload["eta_seconds"] = round(elapsed / done * remaining, 1)
    else:
        payload["eta_seconds"] = None
    return payload


class TaskReporter:
    """Thread-safe reporter handed to the task function."""

    def __init__(self, db, task_id: int, cancel_event: Optional[threading.Event] = None):
        self.db = db
        self.task_id = task_id
        self.cancel_event = cancel_event
        self._lock = threading.Lock()
        self._last_write = 0.0
        self._last_pct = -1.0
        self._last_done = -1
        self._last_message = ""

    @property
    def cancelled(self) -> bool:
        return bool(self.cancel_event and self.cancel_event.is_set())

    def __call__(self, *args, **kwargs) -> None:
        """Allow ``fn(report)`` to call ``report(...)`` like a callback function."""
        self.report(*args, **kwargs)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise TaskCancelled("Task cancelled by user")

    def report(self, done: Optional[int] = None, total: Optional[int] = None,
               message: Optional[str] = None, progress_pct: Optional[float] = None,
               force: bool = False) -> None:
        now = time.monotonic()
        pct = progress_pct
        if pct is None and total:
            pct = (max(0, int(done or 0)) / max(1, int(total))) * 100.0
        with self._lock:
            enough_time = (now - self._last_write) >= _WRITE_INTERVAL_SECONDS
            pct_changed = pct is not None and abs(pct - self._last_pct) >= _PCT_EPSILON
            message_changed = message is not None and message != self._last_message
            if not force and not enough_time and not pct_changed and not message_changed:
                return
            try:
                self.db.update_background_task(
                    self.task_id, done=done, total=total, message=message, progress_pct=pct,
                )
            except Exception as exc:  # progress must never break the task
                logger.debug("Background task %s progress write failed: %s", self.task_id, exc)
            self._last_write = now
            if pct is not None:
                self._last_pct = pct
            if done is not None:
                self._last_done = int(done)
            if message is not None:
                self._last_message = message


class TaskCancelled(Exception):
    pass


def _run_task(db, task_id: int, fn: Callable, cancel_event: Optional[threading.Event]) -> None:
    db.start_background_task(task_id)
    reporter = TaskReporter(db, task_id, cancel_event)
    try:
        result = fn(reporter)
    except TaskCancelled:
        db.finish_background_task(task_id, "cancelled", "Cancelled by user")
        db.log_activity("BACKGROUND_TASK_FINISHED", f"Task #{task_id} cancelled", "cancelled")
        logger.info("Background task #%s cancelled", task_id)
    except Exception as exc:
        db.finish_background_task(task_id, "failed", f"{type(exc).__name__}: {exc}")
        db.log_activity("BACKGROUND_TASK_FINISHED", f"Task #{task_id} failed", str(exc)[:500])
        logger.warning("Background task #%s failed: %s", task_id, exc)
    else:
        db.finish_background_task(task_id, "success", result=result)
        db.log_activity("BACKGROUND_TASK_FINISHED", f"Task #{task_id} finished", json.dumps(result or {}, default=str)[:500])
        logger.info("Background task #%s finished", task_id)
    finally:
        with _LOCK:
            _CANCEL_FLAGS.pop(task_id, None)
            _RUNNING.pop(task_id, None)


def run_in_background(db, kind: str, title: str, fn: Callable, actor: str = "system",
                      scope: Optional[str] = None, cancellable: bool = False) -> Dict[str, Any]:
    """Start *fn(report)* as a persisted background task; never raises."""
    task_id = db.create_background_task(kind, title, scope=scope, actor=actor)
    cancel_event = threading.Event() if cancellable else None
    with _LOCK:
        _RUNNING[task_id] = kind
        if cancel_event is not None:
            _CANCEL_FLAGS[task_id] = cancel_event
    try:
        db.log_activity("BACKGROUND_TASK_STARTED", f"Task #{task_id}: {title}", str(kind))
        _EXECUTOR.submit(_run_task, db, task_id, fn, cancel_event)
    except Exception as exc:  # pragma: no cover - executor failure
        db.finish_background_task(task_id, "failed", f"Could not start task: {exc}")
        logger.error("Could not start background task %s: %s", task_id, exc)
    return {"task_id": task_id, "task": serialize_task(db.get_background_task(task_id))}


def tracked_task(kind: str, title, scope=None):
    """Persist a task around a TaskManager method without changing its signature.

    The wrapped method may call ``self._task_report(...)`` for progress. Titles
    and scopes may be strings or callables ``(manager, *args, **kwargs) -> str``.
    """
    def resolve(value, manager, args, kwargs):
        if callable(value):
            try:
                return str(value(manager, *args, **kwargs) or "")
            except Exception:
                return ""
        return str(value or "")

    def decorate(fn):
        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(self, *args, **kwargs):
                task_id = self.db.create_background_task(
                    kind, resolve(title, self, args, kwargs),
                    scope=resolve(scope, self, args, kwargs),
                )
                self.db.start_background_task(task_id)
                reporter = TaskReporter(self.db, task_id)
                previous = getattr(self, "_task_reporter", None)
                self._task_reporter = reporter
                try:
                    result = await fn(self, *args, **kwargs)
                except TaskCancelled:
                    self.db.finish_background_task(task_id, "cancelled", "Cancelled by user")
                    raise
                except Exception as exc:
                    self.db.finish_background_task(task_id, "failed", f"{type(exc).__name__}: {exc}")
                    raise
                else:
                    if isinstance(result, dict) and str(result.get("status") or "").lower() == "error":
                        self.db.finish_background_task(
                            task_id, "failed", str(result.get("message") or "Task returned an error"),
                            result=result,
                        )
                    else:
                        self.db.finish_background_task(task_id, "success", result=result)
                    return result
                finally:
                    self._task_reporter = previous
            return async_wrapper

        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            task_id = self.db.create_background_task(
                kind, resolve(title, self, args, kwargs),
                scope=resolve(scope, self, args, kwargs),
            )
            self.db.start_background_task(task_id)
            reporter = TaskReporter(self.db, task_id)
            previous = getattr(self, "_task_reporter", None)
            self._task_reporter = reporter
            try:
                result = fn(self, *args, **kwargs)
            except TaskCancelled:
                self.db.finish_background_task(task_id, "cancelled", "Cancelled by user")
                raise
            except Exception as exc:
                self.db.finish_background_task(task_id, "failed", f"{type(exc).__name__}: {exc}")
                raise
            else:
                if isinstance(result, dict) and str(result.get("status") or "").lower() == "error":
                    self.db.finish_background_task(
                        task_id, "failed", str(result.get("message") or "Task returned an error"),
                        result=result,
                    )
                else:
                    self.db.finish_background_task(task_id, "success", result=result)
                return result
            finally:
                self._task_reporter = previous
        return wrapper
    return decorate


def request_cancel(db, task_id: int) -> bool:
    with _LOCK:
        flag = _CANCEL_FLAGS.get(task_id)
    if flag is None:
        return False
    flag.set()
    return True


def cancel_supported(task_id: int) -> bool:
    with _LOCK:
        return task_id in _CANCEL_FLAGS


def active_count(db) -> int:
    return len(db.list_background_tasks(active_only=True, limit=200))


def prune(db, days: int = 7) -> int:
    try:
        return db.prune_background_tasks(days=days)
    except Exception as exc:  # pragma: no cover
        logger.debug("Background task prune failed: %s", exc)
        return 0
