"""
NovelTrax Milestone 4 Iteration 2: Dedicated Empirical Challenger Test Harness
Empirically stress tests:
1. State Machine Ignored Flag Invariants & CRUD Transitions
2. Backfill Engine Concurrency Locking & Race Condition Resilience
3. Backfill Engine Network Resilience (Timeouts, 500/503 HTTP errors, Connection errors)
4. is_pdf_release Delimiters, URL Detection & Inverted Check Verification
5. Dual-Route API Parity & Route Registration Verification
"""

import os
import sys
import tempfile
import sqlite3
import threading
import time
import shutil
import types
from typing import Dict, Any, List, Optional
from unittest.mock import MagicMock, patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

# Ensure DATA_DIR is an isolated temp dir before importing app.main
TMP_GLOBAL_DIR = tempfile.mkdtemp(prefix="noveltrax_challenger_empiric_")
os.environ["DATA_DIR"] = TMP_GLOBAL_DIR

# FastAPI Shim for Calibre Python Runtime if needed
ROUTE_REGISTRY: Dict[tuple, Any] = {}

try:
    import fastapi
    from fastapi import FastAPI, Request, Query, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse, Response
    from fastapi.templating import Jinja2Templates
    from fastapi.staticfiles import StaticFiles
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

    fastapi = types.ModuleType("fastapi")
    fastapi_responses = types.ModuleType("fastapi.responses")
    fastapi_templating = types.ModuleType("fastapi.templating")
    fastapi_staticfiles = types.ModuleType("fastapi.staticfiles")

    class FastAPI:
        def __init__(self, *args, **kwargs):
            self.routes = ROUTE_REGISTRY

        def get(self, path, **kwargs):
            def decorator(f):
                ROUTE_REGISTRY[("GET", path)] = f
                return f
            return decorator

        def post(self, path, **kwargs):
            def decorator(f):
                ROUTE_REGISTRY[("POST", path)] = f
                return f
            return decorator

        def delete(self, path, **kwargs):
            def decorator(f):
                ROUTE_REGISTRY[("DELETE", path)] = f
                return f
            return decorator

        def put(self, path, **kwargs):
            def decorator(f):
                ROUTE_REGISTRY[("PUT", path)] = f
                return f
            return decorator

        def middleware(self, *args, **kwargs):
            return lambda f: f

        def on_event(self, *args, **kwargs):
            return lambda f: f

        def mount(self, *args, **kwargs):
            pass

    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: Any = None):
            self.status_code = status_code
            self.detail = detail
            super().__init__(f"HTTP {status_code}: {detail}")

    class Response:
        def __init__(self, content: Any = None, status_code: int = 200, **kwargs):
            self.content = content
            self.status_code = status_code

    class JSONResponse(Response):
        pass

    class HTMLResponse(Response):
        pass

    class FileResponse(Response):
        pass

    class RedirectResponse(Response):
        pass

    class MockJinja2Templates:
        def __init__(self, *args, **kwargs):
            self.env = type("Env", (), {"filters": {}})()

        def TemplateResponse(self, request=None, name=None, context=None, **kwargs):
            r = Response(status_code=200)
            r.template_name = name
            r.context = context or {}
            return r

        def get_template(self, name):
            return type("Tpl", (), {"render": lambda ctx: f"<!-- Rendered {name} -->"})()

    class MockRequest:
        def __init__(self, query_params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None):
            self.query_params = query_params or {}
            self.headers = headers or {}
            self.state = type("State", (), {})()

    fastapi.FastAPI = FastAPI
    fastapi.Request = MockRequest
    fastapi.Query = lambda default=None, **kw: default
    fastapi.HTTPException = HTTPException
    fastapi_responses.Response = Response
    fastapi_responses.JSONResponse = JSONResponse
    fastapi_responses.HTMLResponse = HTMLResponse
    fastapi_responses.FileResponse = FileResponse
    fastapi_responses.RedirectResponse = RedirectResponse
    fastapi_templating.Jinja2Templates = MockJinja2Templates
    fastapi_staticfiles.StaticFiles = lambda **kw: None

    sys.modules["fastapi"] = fastapi
    sys.modules["fastapi.responses"] = fastapi_responses
    sys.modules["fastapi.templating"] = fastapi_templating
    sys.modules["fastapi.staticfiles"] = fastapi_staticfiles

import app.main

if HAS_FASTAPI:
    for route in app.main.app.routes:
        if hasattr(route, "methods") and hasattr(route, "path") and hasattr(route, "endpoint"):
            for m in route.methods:
                ROUTE_REGISTRY[(m, route.path)] = route.endpoint

from app.database import Database
from app.parser import is_pdf_release, extract_release_group
from app.scheduler import TaskManager


def run_async(coro):
    try:
        return coro.send(None)
    except StopIteration as e:
        return e.value
    except Exception:
        raise


class TestResultTracker:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors = []

    def check(self, condition, message, details=None):
        if condition:
            self.passed += 1
            print(f"  [PASS] {message}")
        else:
            self.failed += 1
            err_msg = f"  [FAIL] {message}"
            if details:
                err_msg += f" (Details: {details})"
            print(err_msg)
            self.errors.append(err_msg)


def run_state_machine_tests(t: TestResultTracker):
    print("\n" + "=" * 70)
    print(" 1. EMPIRICAL STATE MACHINE & IGNORED INVARIANT STRESS TESTS")
    print("=" * 70)

    temp_dir = tempfile.mkdtemp(prefix="noveltrax_sm_test_")
    db_path = os.path.join(temp_dir, "test_sm.db")
    try:
        db = Database(db_path)
        db.init_db()

        # Add tracked series via sync_calibre_series
        db.sync_calibre_series([{
            "series_name": "Overlord",
            "series_id": 1,
            "book_count": 15,
            "latest_index": 15.0,
            "authors": "Kugane Maruyama"
        }])

        # 1.1 Insert a release with REQUESTED
        is_new, rel_id = db.upsert_release({
            "guid": "guid-test-sm-1",
            "title": "[Yen Press] Overlord Vol. 16 [EPUB]",
            "series_name": "Overlord",
            "volume": 16.0,
            "status": "REQUESTED",
            "release_group": "Yen Press"
        })
        t.check(is_new and rel_id > 0, "Insert initial release with REQUESTED")

        r1 = db.get_release_by_id(rel_id)
        t.check(r1["status"] == "REQUESTED", "Initial status is REQUESTED", r1["status"])
        t.check(r1["ignored"] == 0 or r1["ignored"] is False, "Initial ignored flag is 0", r1["ignored"])

        # 1.2 Transition REQUESTED -> IGNORED via ignore_release
        ok = db.ignore_release(rel_id)
        t.check(ok, "ignore_release returns True")
        r_ign = db.get_release_by_id(rel_id)
        t.check(r_ign["status"] == "IGNORED", "Status is IGNORED after ignore_release", r_ign["status"])
        t.check(bool(r_ign["ignored"]), "ignored flag is 1 after ignore_release", r_ign["ignored"])

        # Query visibility while IGNORED
        active_rels = db.get_releases()
        t.check(rel_id not in [x["id"] for x in active_rels], "IGNORED release excluded from default get_releases()")

        all_rels = db.get_releases(include_ignored=True)
        t.check(rel_id in [x["id"] for x in all_rels], "IGNORED release included in get_releases(include_ignored=True)")

        ign_rels = db.get_releases(status="IGNORED")
        t.check(rel_id in [x["id"] for x in ign_rels], "IGNORED release returned by get_releases(status='IGNORED')")

        grouped_active = db.get_releases_grouped_by_series()
        t.check("Overlord" not in [g["series_name"] for g in grouped_active], "IGNORED series excluded from default grouped view")

        grouped_all = db.get_releases_grouped_by_series(include_ignored=True)
        t.check("Overlord" in [g["series_name"] for g in grouped_all], "IGNORED series included in grouped view with include_ignored=True")

        # 1.3 Transition IGNORED -> IN_CALIBRE via update_release_status
        db.update_release_status(rel_id, "IN_CALIBRE", matched_book_id=999)
        r_cal = db.get_release_by_id(rel_id)
        t.check(r_cal["status"] == "IN_CALIBRE", "Status updated to IN_CALIBRE", r_cal["status"])
        t.check(r_cal["ignored"] == 0 or r_cal["ignored"] is False, "ignored flag reset to 0 on IN_CALIBRE transition", r_cal["ignored"])
        t.check(r_cal["calibre_matched_book_id"] == 999, "calibre_matched_book_id stored", r_cal["calibre_matched_book_id"])

        cal_rels = db.get_releases(status="IN_CALIBRE")
        t.check(rel_id in [x["id"] for x in cal_rels], "Release visible in get_releases(status='IN_CALIBRE')", [x["id"] for x in cal_rels])

        active_rels_cal = db.get_releases()
        t.check(rel_id in [x["id"] for x in active_rels_cal], "Release visible in default get_releases() after IN_CALIBRE")

        # 1.4 Transition IN_CALIBRE -> IGNORED via update_release_status('IGNORED')
        db.update_release_status(rel_id, "IGNORED")
        r_ign2 = db.get_release_by_id(rel_id)
        t.check(r_ign2["status"] == "IGNORED", "Status transitioned to IGNORED via update_release_status", r_ign2["status"])
        t.check(bool(r_ign2["ignored"]), "ignored flag set to 1 via update_release_status('IGNORED')", r_ign2["ignored"])

        # 1.5 Transition IGNORED -> REQUESTED via update_release_status('REQUESTED')
        db.update_release_status(rel_id, "REQUESTED")
        r_req = db.get_release_by_id(rel_id)
        t.check(r_req["status"] == "REQUESTED", "Status transitioned to REQUESTED via update_release_status", r_req["status"])
        t.check(r_req["ignored"] == 0 or r_req["ignored"] is False, "ignored flag reset to 0 via update_release_status('REQUESTED')", r_req["ignored"])

        # 1.6 Transition REQUESTED -> IGNORED -> unignore_release -> REQUESTED
        db.ignore_release(rel_id)
        ok_unign = db.unignore_release(rel_id)
        t.check(ok_unign, "unignore_release returns True")
        r_restored = db.get_release_by_id(rel_id)
        t.check(r_restored["status"] == "REQUESTED", "Status restored to REQUESTED after unignore_release", r_restored["status"])
        t.check(r_restored["ignored"] == 0 or r_restored["ignored"] is False, "ignored flag reset to 0 after unignore_release", r_restored["ignored"])

        # 1.7 Transition IGNORED -> NEW via update_release_status('NEW')
        db.ignore_release(rel_id)
        db.update_release_status(rel_id, "NEW")
        r_new = db.get_release_by_id(rel_id)
        t.check(r_new["status"] == "NEW", "Status transitioned to NEW via update_release_status", r_new["status"])
        t.check(r_new["ignored"] == 0 or r_new["ignored"] is False, "ignored flag reset to 0 via update_release_status('NEW')", r_new["ignored"])

        # 1.8 Startup Self-Healing Migration: Invariant repair
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE releases SET status = 'IN_CALIBRE', ignored = 1 WHERE id = ?;", (rel_id,))
            conn.commit()

        corrupted = db.get_release_by_id(rel_id)
        t.check(corrupted["status"] == "IN_CALIBRE" and bool(corrupted["ignored"]), "Record artificially corrupted with dirty ignored=1")

        db.init_db()
        healed = db.get_release_by_id(rel_id)
        t.check(healed["ignored"] == 0 or healed["ignored"] is False, "init_db self-healing reset dirty ignored flag to 0 for IN_CALIBRE", healed["ignored"])

        # 1.9 Feed Rescrape Preserves IGNORED status
        db.ignore_release(rel_id)
        is_new_crawl, rel_id_crawl = db.upsert_release({
            "guid": "guid-test-sm-1",
            "title": "[Yen Press] Overlord Vol. 16 [EPUB] (Updated)",
            "series_name": "Overlord",
            "volume": 16.0,
            "status": "REQUESTED"
        })
        t.check(not is_new_crawl and rel_id_crawl == rel_id, "Re-crawled item identified as existing")
        r_rescraped = db.get_release_by_id(rel_id)
        t.check(r_rescraped["status"] == "IGNORED", "Existing IGNORED status preserved across feed rescrapes", r_rescraped["status"])
        t.check(bool(r_rescraped["ignored"]), "Existing ignored=1 preserved across feed rescrapes", r_rescraped["ignored"])

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_backfill_concurrency_and_network_tests(t: TestResultTracker):
    print("\n" + "=" * 70)
    print(" 2. BACKFILL ENGINE CONCURRENCY LOCKING & NETWORK RESILIENCE")
    print("=" * 70)

    temp_dir = tempfile.mkdtemp(prefix="noveltrax_bf_test_")
    db_path = os.path.join(temp_dir, "test_bf.db")
    try:
        db = Database(db_path)
        db.init_db()
        db.sync_calibre_series([{
            "series_name": "DanMachi",
            "series_id": 2,
            "book_count": 18,
            "latest_index": 18.0,
            "authors": "Fujino Omori"
        }])

        tm = TaskManager(db, calibre_path=temp_dir)

        # 2.1 Concurrency Lock Stress Test: Multiple simultaneous threads
        lock_results = []
        barrier = threading.Barrier(10)

        # Deterministic handshake instead of a fixed sleep: the worker parks inside
        # the mocked provider dispatch until this test releases it. Relying on
        # time.sleep() here is a race -- the worker performs three DB writes before
        # it reaches backfill_series(), so a short sleep could lapse while the
        # ``with patch.object(...)`` block was already unwinding, letting the worker
        # fall through to the real (network) backfill and leaving the lock held.
        entered_evt = threading.Event()
        release_evt = threading.Event()

        def mock_backfill_slow(series_name, max_pages=3):
            entered_evt.set()
            release_evt.wait(timeout=10)
            return {"status": "success", "message": "done", "series_name": series_name, "releases_found": 1, "requested": 1}

        def wait_until(predicate, timeout=10.0, interval=0.01):
            deadline = time.time() + timeout
            while time.time() < deadline:
                if predicate():
                    return True
                time.sleep(interval)
            return predicate()

        with patch.object(tm, "backfill_series", side_effect=mock_backfill_slow):
            def trigger_backfill(idx):
                barrier.wait()
                res = tm.run_series_backfill_async(f"Series_{idx}", max_pages=2)
                lock_results.append(res)

            threads = [threading.Thread(target=trigger_backfill, args=(i,)) for i in range(10)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()

            started_count = sum(1 for r in lock_results if r.get("status") == "started")
            in_progress_count = sum(1 for r in lock_results if r.get("status") == "in_progress")

            t.check(started_count == 1, f"Exactly 1 thread acquired backfill lock (got {started_count})", started_count)
            t.check(in_progress_count == 9, f"Exactly 9 threads rejected with 'in_progress' (got {in_progress_count})", in_progress_count)

            # The lock must still be held while the worker is genuinely running.
            t.check(entered_evt.wait(timeout=10), "Backfill worker reached provider dispatch (mock never called)")
            t.check(tm.is_backfilling, "Lock held while the worker is still running")

            # Release the worker, then wait for the lock to clear (bounded poll).
            release_evt.set()
            t.check(wait_until(lambda: not tm.is_backfilling), "is_backfilling flag reset to False after worker completes")
            t.check(wait_until(lambda: db.get_setting("backfill_status") == "idle"), "backfill_status in DB returned to 'idle'", db.get_setting("backfill_status"))

            # Subsequent backfill immediately succeeds after lock release
            entered_evt.clear()
            release_evt.clear()
            subsequent_res = tm.run_series_backfill_async("DanMachi", max_pages=1)
            t.check(subsequent_res.get("status") == "started", "Subsequent backfill succeeds after prior worker released lock", subsequent_res)
            t.check(entered_evt.wait(timeout=10), "Subsequent backfill worker reached provider dispatch")
            release_evt.set()
            t.check(wait_until(lambda: not tm.is_backfilling), "Lock released cleanly after subsequent backfill")

        # 2.2 Network Resilience: Timeouts, ConnectionError, OSError, Panic
        import socket
        try:
            import httpx
            httpx_err = httpx.TimeoutException("Read timed out")
        except ImportError:
            httpx_err = None

        error_scenarios = [
            ("socket.timeout", socket.timeout("Socket read timed out")),
            ("ConnectionError", ConnectionError("Failed to establish a new connection")),
            ("OSError (Network down)", OSError(10051, "Network is unreachable")),
            ("TimeoutError", TimeoutError("Operation timed out")),
            ("RuntimeError Provider Panic", RuntimeError("Unexpected parser panic inside provider")),
        ]
        if httpx_err:
            error_scenarios.append(("httpx.TimeoutException", httpx_err))

        for err_name, exc in error_scenarios:
            with patch("app.providers.nyaa.NyaaNovelProvider.search_series", side_effect=exc):
                try:
                    res = tm.backfill_series("DanMachi", max_pages=1)
                    t.check(res.get("status") == "success", f"Resilience against {err_name}: returns success status dict", res.get("status"))
                    t.check(res.get("releases_found") == 0, f"Resilience against {err_name}: 0 releases found", res.get("releases_found"))
                except Exception as ex:
                    t.check(False, f"Crash under {err_name}: unhandled exception {ex}")

        # 2.3 Early Termination via stop_backfill_requested
        class MockRelease:
            def __init__(self, idx):
                self.title = f"[Yen Press] DanMachi Vol {idx} [EPUB]"
                self.link = f"https://example.com/item/{idx}"
                self.guid = f"guid-dm-{idx}"
                self.volume = float(idx)
                self.cover_url = None
            def to_dict(self):
                return {
                    "title": self.title,
                    "link": self.link,
                    "guid": self.guid,
                    "volume": self.volume,
                    "series_name": "DanMachi",
                    "release_group": "Yen Press"
                }

        mock_items = [MockRelease(i) for i in range(1, 10)]

        def search_series_with_stop(series_name, max_pages=3, stop_check=None):
            results = []
            for item in mock_items:
                if stop_check and stop_check():
                    break
                results.append(item)
            return results

        tm.stop_backfill_requested = True
        with patch("app.providers.nyaa.NyaaNovelProvider.search_series", side_effect=search_series_with_stop):
            res_stopped = tm.backfill_series("DanMachi", max_pages=3)
            t.check(res_stopped.get("releases_found") == 0, "Early termination: found 0 items when stop_backfill_requested=True", res_stopped.get("releases_found"))

        tm.stop_backfill_requested = False

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_pdf_delimiter_and_inverted_check_tests(t: TestResultTracker):
    print("\n" + "=" * 70)
    print(" 3. PDF URL DELIMITERS & INVERTED CHECK EMPIRICAL VERIFICATION")
    print("=" * 70)

    bare_pdf_cases = [
        ("Overlord Vol 16 (Digital)", "https://nyaa.si/download/12345.pdf", None, True, "Bare .pdf URL with title text"),
        ("DanMachi Vol 19", None, "https://nyaa.si/download/67890.pdf", True, "Bare .pdf in guid with title text"),
        (None, "https://nyaa.si/download/12345.pdf", None, True, "Bare .pdf URL without title"),
        ("Overlord Vol 16", "https://nyaa.si/download/12345.pdf?download=true", None, True, ".pdf followed by query param '?'"),
        ("Overlord Vol 16", "https://nyaa.si/download/12345.pdf#page=1", None, True, ".pdf followed by hash '#"),
        ("Overlord Vol 16", "https://nyaa.si/download/12345.pdf&dl=1", None, True, ".pdf followed by amp '&'"),
        ("Overlord Vol 16", "https://nyaa.si/download/12345.pdf/view", None, True, ".pdf followed by slash '/'"),
        ("Overlord Vol 16", "https://nyaa.si/view/12345?format=pdf", None, True, "format=pdf in URL parameter"),
        ("Overlord Vol 16", "https://nyaa.si/view/12345?ext=pdf", None, True, "ext=pdf in URL parameter"),
        ("Overlord Vol 16", "https://nyaa.si/view/12345/pdf/download", None, True, "/pdf/ path element in URL"),
        ("[PDF] Overlord Vol 16 [EPUB]", "https://nyaa.si/download/123.torrent", None, True, "Bracketed [PDF] in title"),
        ("Overlord Vol 16 (PDF)", "https://nyaa.si/download/123.torrent", None, True, "Parenthesized (PDF) in title"),
        ("Overlord Vol 16.pdf", "https://nyaa.si/download/123.torrent", None, True, "Title ends with .pdf"),
        ("Overlord Vol 16 (PDF+EPUB)", "https://nyaa.si/download/123.torrent", None, True, "Hybrid (PDF+EPUB) in title"),
        ("Overlord Vol 16 [PDF + EPUB]", "https://nyaa.si/download/123.torrent", None, True, "Hybrid [PDF + EPUB] in title"),
        ("[Yen Press] Overlord Vol. 16 [EPUB]", "https://nyaa.si/download/12345.torrent", None, False, "Legitimate EPUB release"),
        ("DanMachi Vol 19 [Digital]", "magnet:?xt=urn:btih:ABCDEF&dn=DanMachi+Vol+19", None, False, "Magnet link without PDF"),
        ("Ascendance of a Bookworm Vol 1", "https://nyaa.si/download/123.epub", None, False, "Explicit .epub URL"),
        ("The Spider and the Butterfly Vol 1", "https://nyaa.si/view/12345", None, False, "Standard web link"),
    ]

    for title, link, guid, expected, desc in bare_pdf_cases:
        res = is_pdf_release(title, link, guid)
        t.check(res is expected, f"{desc} -> is_pdf_release={expected} (got {res})")

    # Inverted assertion verification from test_challenger_m4_integration.py line 490
    pdf_space_boundary = is_pdf_release("Title Has Text", "https://nyaa.si/download/123.pdf")
    t.check(pdf_space_boundary is True, "Inverted check: is_pdf_release correctly identifies bare .pdf link with title text as True")


def run_api_parity_and_endpoints_tests(t: TestResultTracker):
    print("\n" + "=" * 70)
    print(" 4. DUAL-ROUTE API PARITY & ROUTE REGISTRY VERIFICATION")
    print("=" * 70)

    # Verify route registrations in ROUTE_REGISTRY
    expected_routes = [
        ("POST", "/api/series/{series_id}/backfill"),
        ("POST", "/api/series/backfill"),
        ("GET", "/api/series_releases"),
        ("GET", "/api/releases/grouped"),
        ("POST", "/api/releases/{release_id}/ignore"),
        ("POST", "/api/releases/{release_id}/unignore"),
        ("GET", "/prioritized"),
        ("GET", "/api/prioritized/releases"),
    ]

    for meth, path in expected_routes:
        t.check((meth, path) in ROUTE_REGISTRY, f"Route {meth} {path} registered in app")

    # Direct handler invocation for endpoints
    handler_bf_id = ROUTE_REGISTRY.get(("POST", "/api/series/{series_id}/backfill"))
    handler_bf_name = ROUTE_REGISTRY.get(("POST", "/api/series/backfill"))
    handler_ign = ROUTE_REGISTRY.get(("POST", "/api/releases/{release_id}/ignore"))
    handler_unign = ROUTE_REGISTRY.get(("POST", "/api/releases/{release_id}/unignore"))
    handler_sr = ROUTE_REGISTRY.get(("GET", "/api/series_releases"))
    handler_rg = ROUTE_REGISTRY.get(("GET", "/api/releases/grouped"))

    # Test /api/series/backfill with empty name -> HTTP 400
    if handler_bf_name:
        res_empty = run_async(handler_bf_name({"series_name": ""}))
        t.check(getattr(res_empty, "status_code", None) == 400, "POST /api/series/backfill with empty name returns 400")

    # Test /api/series/{series_id}/backfill with non-existent ID -> HTTP 404
    if handler_bf_id:
        threw_404 = False
        try:
            run_async(handler_bf_id(9999999))
        except Exception as e:
            if hasattr(e, "status_code") and e.status_code == 404:
                threw_404 = True
        t.check(threw_404, "POST /api/series/9999999/backfill raises 404 for unknown series")

    # Test Dual Route Parity: handler_sr and handler_rg point to same logic / return parity
    t.check(handler_sr is not None and handler_rg is not None, "Both /api/series_releases and /api/releases/grouped handlers exist")
    t.check(handler_sr == handler_rg, "Route alias parity: handler_sr is exactly handler_rg")


def main():
    tracker = TestResultTracker()
    run_state_machine_tests(tracker)
    run_backfill_concurrency_and_network_tests(tracker)
    run_pdf_delimiter_and_inverted_check_tests(tracker)
    run_api_parity_and_endpoints_tests(tracker)

    print("\n" + "=" * 70)
    print(" EMPIRICAL CHALLENGER SUMMARY")
    print("=" * 70)
    print(f" Total Checks Executed : {tracker.passed + tracker.failed}")
    print(f" Passed                : {tracker.passed}")
    print(f" Failed                : {tracker.failed}")
    print("=" * 70)

    if tracker.failed > 0:
        print("\n❌ VERDICT: REQUEST_CHANGES — Failures encountered:")
        for err in tracker.errors:
            print(f"  {err}")
        sys.exit(1)
    else:
        print("\n✅ VERDICT: APPROVE — All empirical stress tests passed with 100% success!")
        sys.exit(0)


if __name__ == "__main__":
    main()
