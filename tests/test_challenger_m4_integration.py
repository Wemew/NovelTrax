#!/usr/bin/env python3
r"""
tests/test_challenger_m4_integration.py
Adversarial Challenger E2E Integration & Stress Harness for NovelTrax Milestone 4:
System-Wide E2E Integration, Backfill Engine Stress, Full API Surface Testing & Zero-Regression Verification.

Execution:
  & "C:\Program Files\Calibre2\calibre-debug.exe" -e tests\test_challenger_m4_integration.py
"""

import os
import sys
import json
import time
import shutil
import socket
import sqlite3
import tempfile
import threading
import types
import asyncio
from typing import Dict, Any, List, Optional
from unittest.mock import patch, MagicMock

# -----------------------------------------------------------------------------
# Path & Environment Setup
# -----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

# Temporary directory for isolated runtime database
TMP_RUN_DIR = tempfile.mkdtemp(prefix="noveltrax_challenger_m4_")
os.environ["DATA_DIR"] = TMP_RUN_DIR

# -----------------------------------------------------------------------------
# FastAPI Shim for Calibre Python Runtime (if not present)
# -----------------------------------------------------------------------------
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
            if isinstance(content, (dict, list)):
                self.body = json.dumps(content).encode("utf-8")
            elif isinstance(content, str):
                self.body = content.encode("utf-8")
            elif isinstance(content, bytes):
                self.body = content
            else:
                self.body = b""

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

# Import main application module
import app.main

# Ensure app routes populated in ROUTE_REGISTRY if FastAPI was real
if HAS_FASTAPI:
    for route in app.main.app.routes:
        if hasattr(route, "methods") and hasattr(route, "path") and hasattr(route, "endpoint"):
            for m in route.methods:
                ROUTE_REGISTRY[(m, route.path)] = route.endpoint

# Project imports
from tests.mock_calibre import create_mock_calibre_db
from app.database import Database
from app.scheduler import TaskManager
from app.calibre.scanner import CalibreScanner, parse_calibre_volumes_and_series
from app.calibre.matcher import CalibreMatcher
from app.downloader.qbit import parse_torrent_filename_volumes, QBitClient, SelectiveDownloadResult
from app.security import is_safe_public_url
from app.parser import is_pdf_release, extract_release_group, extract_series_and_volume
from app.providers.base import ReleaseItem


# -----------------------------------------------------------------------------
# Non-strippable Challenger Recorder Pattern (Calibre -O2 Proof)
# -----------------------------------------------------------------------------
class ChallengerRecorder:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures: List[Dict[str, str]] = []

    def check(self, condition: bool, test_name: str, details: str = "") -> bool:
        if condition:
            self.passed += 1
            print(f"  [PASS] {test_name}")
            return True
        else:
            self.failed += 1
            self.failures.append({"test": test_name, "details": details})
            print(f"  [FAIL] {test_name}\n         Details: {details}")
            return False


def run_async(coro):
    """Executes a coroutine safely without Windows IOCP deadlocks in embedded Calibre."""
    try:
        return coro.send(None)
    except StopIteration as e:
        return e.value
    except Exception:
        raise


def get_response_data(response: Any) -> Any:
    """Extracts JSON dict/list from response object regardless of framework implementation."""
    if hasattr(response, "content") and isinstance(response.content, (dict, list)):
        return response.content
    if hasattr(response, "body") and response.body:
        try:
            return json.loads(response.body.decode("utf-8"))
        except Exception:
            pass
    return None


# =============================================================================
# PART 1: TARGETED SERIES BACKFILL INTEGRATION & STRESS HARNESS
# =============================================================================
def run_part_1_backfill_stress(r: ChallengerRecorder):
    print("\n" + "=" * 78)
    print("  PART 1: TARGETED SERIES BACKFILL INTEGRATION & STRESS HARNESS")
    print("=" * 78)

    tmp_dir = tempfile.mkdtemp(prefix="noveltrax_test_backfill_")
    calibre_db_file = os.path.join(tmp_dir, "calibre", "metadata.db")
    app_db_file = os.path.join(tmp_dir, "app", "noveltrax.db")

    try:
        # Step A: Seed Mock Calibre Library (Overlord has Vols 1..15)
        create_mock_calibre_db(calibre_db_file)
        calibre_dir = os.path.dirname(calibre_db_file)
        scanner = CalibreScanner(calibre_dir)
        series_list = scanner.get_all_series()

        # Step B: Initialize App Database and sync series
        db = Database(app_db_file)
        db.sync_calibre_series(series_list)
        tm = TaskManager(db, calibre_dir)

        # ---------------------------------------------------------------------
        # 1.1 Multi-Volume Backfill with Missing Volume Matching & Database Insertion
        # ---------------------------------------------------------------------
        print("\n--- 1.1 Missing Volume Detection & Database Insertion ---")

        # Mock Nyaa candidate releases for Overlord:
        # - Vol 14: Already present in Calibre (Book #32) -> Should be IN_CALIBRE
        # - Vol 15: Present in Calibre (Book #33) -> Should be IN_CALIBRE
        # - Vol 16: Not in Calibre -> Should be REQUESTED
        # - Vol 17: Not in Calibre -> Should be REQUESTED
        mock_candidates = [
            ReleaseItem(
                guid="nyaa-overlord-14",
                title="[Yen Press] Overlord, Vol. 14 [Digital] [EPUB]",
                series_name="Overlord",
                volume=14.0,
                link="https://nyaa.si/view/100014",
                download_url="magnet:?xt=urn:btih:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                source_feed_name="Nyaa",
                raw_data={"seeders": 25, "size": "25.0 MiB"},
                release_group="Yen Press"
            ),
            ReleaseItem(
                guid="nyaa-overlord-15",
                title="[Yen Press] Overlord, Vol. 15 [Digital] [EPUB]",
                series_name="Overlord",
                volume=15.0,
                link="https://nyaa.si/view/100015",
                download_url="magnet:?xt=urn:btih:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
                source_feed_name="Nyaa",
                raw_data={"seeders": 35, "size": "26.0 MiB"},
                release_group="Yen Press"
            ),
            ReleaseItem(
                guid="nyaa-overlord-16",
                title="[Yen Press] Overlord, Vol. 16 [Digital] [EPUB]",
                series_name="Overlord",
                volume=16.0,
                link="https://nyaa.si/view/100016",
                download_url="magnet:?xt=urn:btih:CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
                source_feed_name="Nyaa",
                raw_data={"seeders": 45, "size": "28.0 MiB"},
                release_group="Yen Press"
            ),
            ReleaseItem(
                guid="nyaa-overlord-17",
                title="[Stick] Overlord, Vol. 17 [Digital] [EPUB]",
                series_name="Overlord",
                volume=17.0,
                link="https://nyaa.si/view/100017",
                download_url="magnet:?xt=urn:btih:DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD",
                source_feed_name="Nyaa",
                raw_data={"seeders": 18, "size": "24.5 MiB"},
                release_group="Stick"
            ),
        ]

        with patch("app.providers.nyaa.NyaaNovelProvider.search_series", return_value=mock_candidates):
            bf_result = tm.backfill_series("Overlord", max_pages=3)

        r.check(bf_result.get("status") == "success", "backfill_series returned status 'success'")
        r.check(bf_result.get("releases_found") == 4, f"Found 4 candidates, got {bf_result.get('releases_found')}")
        r.check(bf_result.get("requested") == 2, f"Requested exactly 2 missing volumes, got {bf_result.get('requested')}")

        # Check DB releases
        all_rels = {r_item["guid"]: r_item for r_item in db.get_releases(include_ignored=True)}
        r.check("nyaa-overlord-14" in all_rels, "Vol 14 inserted into database")
        r.check(all_rels["nyaa-overlord-14"]["status"] == "IN_CALIBRE",
                f"Vol 14 status is IN_CALIBRE (found {all_rels['nyaa-overlord-14']['status']})")
        r.check(all_rels["nyaa-overlord-14"]["release_group"] == "Yen Press", "Vol 14 release_group is 'Yen Press'")

        r.check("nyaa-overlord-16" in all_rels, "Vol 16 inserted into database")
        r.check(all_rels["nyaa-overlord-16"]["status"] == "REQUESTED",
                f"Missing Vol 16 status is REQUESTED (found {all_rels['nyaa-overlord-16']['status']})")
        r.check(all_rels["nyaa-overlord-16"]["release_group"] == "Yen Press", "Vol 16 release_group is 'Yen Press'")

        r.check("nyaa-overlord-17" in all_rels, "Vol 17 inserted into database")
        r.check(all_rels["nyaa-overlord-17"]["status"] == "REQUESTED", "Missing Vol 17 status is REQUESTED")
        r.check(all_rels["nyaa-overlord-17"]["release_group"] == "Stick", "Vol 17 release_group is 'Stick'")

        # Setting verification
        r.check(db.get_setting("backfill_last_page") == "3", "backfill_last_page setting updated to '3'")

        # Activity log verification
        logs = db.get_logs(limit=5)
        has_bf_log = any(log["event_type"] == "SERIES_BACKFILLED" for log in logs)
        r.check(has_bf_log, "SERIES_BACKFILLED event recorded in activity logs")

        # ---------------------------------------------------------------------
        # 1.2 Asynchronous Backfill Worker & Concurrency Collision Lock
        # ---------------------------------------------------------------------
        print("\n--- 1.2 Async Backfill Worker & Concurrency Guard ---")

        # Start asynchronous worker
        with patch("app.providers.nyaa.NyaaNovelProvider.search_series", return_value=mock_candidates):
            async_res = tm.run_series_backfill_async("Overlord", max_pages=2)
            r.check(async_res.get("status") == "started", f"Async launcher returns status 'started': {async_res}")

            # Collision test: attempt second backfill while first is in-progress
            collision_res = tm.run_series_backfill_async("DanMachi", max_pages=1)
            r.check(collision_res.get("status") == "in_progress",
                    f"Concurrent backfill request rejected with 'in_progress': {collision_res}")

            # Wait for daemon worker to complete
            time.sleep(0.3)
            timeout_counter = 0
            while tm.is_backfilling and timeout_counter < 30:
                time.sleep(0.1)
                timeout_counter += 1

            r.check(not tm.is_backfilling, "Async backfill thread completed cleanly")
            r.check(db.get_setting("backfill_status") == "idle",
                    f"backfill_status transitioned to 'idle': {db.get_setting('backfill_status')}")

        # ---------------------------------------------------------------------
        # 1.3 Error Resilience: Network Timeouts & Empty Searches
        # ---------------------------------------------------------------------
        print("\n--- 1.3 Error Resilience: Network Timeouts & Empty Results ---")

        # A. Network Timeout
        with patch("app.providers.nyaa.NyaaNovelProvider.search_series", side_effect=socket.timeout("Mock network timeout")):
            timeout_res = tm.backfill_series("Overlord", max_pages=2)
            r.check(timeout_res.get("status") == "success", "Network timeout handled gracefully without unhandled exception")
            r.check(timeout_res.get("releases_found") == 0, "0 releases found on network timeout")

        # B. Generic Network Error
        with patch("app.providers.nyaa.NyaaNovelProvider.search_series", side_effect=ConnectionError("DNS resolution failed")):
            conn_err_res = tm.backfill_series("Overlord", max_pages=2)
            r.check(conn_err_res.get("status") == "success", "ConnectionError handled gracefully")
            r.check(conn_err_res.get("releases_found") == 0, "0 releases found on ConnectionError")

        # C. Empty Provider Search
        with patch("app.providers.nyaa.NyaaNovelProvider.search_series", return_value=[]):
            empty_res = tm.backfill_series("NonExistentSeries", max_pages=1)
            r.check(empty_res.get("status") == "success", "Empty search returns success")
            r.check(empty_res.get("releases_found") == 0, "0 releases found on empty provider result")
            r.check(empty_res.get("requested") == 0, "0 requested on empty provider result")

        # ---------------------------------------------------------------------
        # 1.4 Malformed and Adversarial Candidate Releases
        # ---------------------------------------------------------------------
        print("\n--- 1.4 Malformed & Adversarial Release Filtering ---")

        adversarial_items = [
            # PDF item: should be blocked by is_pdf_release
            ReleaseItem(
                guid="mal-pdf-1",
                title="[Yen Press] Overlord Vol 18 [PDF]",
                series_name="Overlord",
                volume=18.0,
                link="https://nyaa.si/view/100018",
                download_url="magnet:?xt=urn:btih:EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE",
                release_group="Yen Press"
            ),
            # PDF link item with /pdf/ in URL
            ReleaseItem(
                guid="mal-pdf-2",
                title="[Yen Press] Overlord Vol 19 [EPUB]",
                series_name="Overlord",
                volume=19.0,
                link="https://nyaa.si/pdf/100019",
                download_url="https://nyaa.si/pdf/100019",
                release_group="Yen Press"
            ),
            # SQL Injection payload in release title
            ReleaseItem(
                guid="mal-sqli-1",
                title="[Yen Press] Overlord Vol 20'; DROP TABLE releases;-- [EPUB]",
                series_name="Overlord",
                volume=20.0,
                link="https://nyaa.si/view/100020",
                download_url="magnet:?xt=urn:btih:FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",
                release_group="Yen Press"
            ),
            # HTML / XSS script tag in release title
            ReleaseItem(
                guid="mal-xss-1",
                title="[Yen Press] Overlord Vol 21 <script>alert('xss')</script> [EPUB]",
                series_name="Overlord",
                volume=21.0,
                link="https://nyaa.si/view/100021",
                download_url="magnet:?xt=urn:btih:0000000000000000000000000000000000000000",
                release_group="Yen Press"
            ),
            # Extreme decimal volume
            ReleaseItem(
                guid="adv-dec-1",
                title="[Yen Press] Overlord Vol 22.5 [EPUB]",
                series_name="Overlord",
                volume=22.5,
                link="https://nyaa.si/view/100022",
                download_url="magnet:?xt=urn:btih:1111111111111111111111111111111111111111",
                release_group="Yen Press"
            ),
            # Multi-volume bundle in backfill
            ReleaseItem(
                guid="adv-bundle-1",
                title="[Yen Press] Overlord Vol 23-25 [EPUB]",
                series_name="Overlord",
                volume=23.0,
                link="https://nyaa.si/view/100023",
                download_url="magnet:?xt=urn:btih:2222222222222222222222222222222222222222",
                is_bundle=True,
                volume_start=23.0,
                volume_end=25.0,
                volumes=[23.0, 24.0, 25.0],
                release_group="Yen Press"
            ),
            # Release with empty/unknown release group
            ReleaseItem(
                guid="adv-nogroup-1",
                title="Overlord Vol 26 [EPUB]",
                series_name="Overlord",
                volume=26.0,
                link="https://nyaa.si/view/100026",
                download_url="magnet:?xt=urn:btih:3333333333333333333333333333333333333333",
                release_group=""
            )
        ]

        with patch("app.providers.nyaa.NyaaNovelProvider.search_series", return_value=adversarial_items):
            adv_res = tm.backfill_series("Overlord", max_pages=1)

        # PDF releases must be skipped
        r.check("mal-pdf-1" not in {x["guid"] for x in db.get_releases(include_ignored=True)},
                "PDF title release mal-pdf-1 was strictly rejected from database")
        r.check("mal-pdf-2" not in {x["guid"] for x in db.get_releases(include_ignored=True)},
                "PDF path release mal-pdf-2 was strictly rejected from database")

        # Adversarial parser analysis: is_pdf_release path vs title vs trailing space behavior
        r.check(is_pdf_release("Overlord Vol 19 [PDF]"), "is_pdf_release rejects bracketed [PDF] title")
        r.check(is_pdf_release("Novel Title.pdf"), "is_pdf_release rejects filename ending with .pdf")
        r.check(is_pdf_release(link="https://nyaa.si/pdf/100019"), "is_pdf_release rejects /pdf/ path in URL")
        r.check(is_pdf_release(link="https://nyaa.si/download?format=pdf"), "is_pdf_release rejects format=pdf in URL")

        # Invert empirical caveat check once is_pdf_release correctly identifies bare .pdf links
        pdf_space_boundary = is_pdf_release(raw_title="Overlord Vol 19 [EPUB]", link="https://nyaa.si/download/100019.pdf")
        r.check(pdf_space_boundary,
                "Fixed: is_pdf_release correctly identifies bare .pdf link even when title has text")

        # SQL Injection resilience
        sqli_rel = next((x for x in db.get_releases(include_ignored=True) if x["guid"] == "mal-sqli-1"), None)
        r.check(sqli_rel is not None, "SQL Injection payload item safely parameterized and stored")
        r.check(db.count_releases() > 0, "Database releases table intact (no SQL injection drop)")

        # XSS resilience
        xss_rel = next((x for x in db.get_releases(include_ignored=True) if x["guid"] == "mal-xss-1"), None)
        r.check(xss_rel is not None, "XSS payload item stored cleanly without schema error")

        # Decimal volume handling
        dec_rel = next((x for x in db.get_releases(include_ignored=True) if x["guid"] == "adv-dec-1"), None)
        r.check(dec_rel is not None and dec_rel["volume"] == 22.5, "Decimal volume 22.5 preserved accurately")

        # Bundle in backfill
        bun_rel = next((x for x in db.get_releases(include_ignored=True) if x["guid"] == "adv-bundle-1"), None)
        r.check(bun_rel is not None and bun_rel["status"] == "REQUESTED",
                "Unowned multi-volume bundle evaluated as REQUESTED")

        # No group tag release
        nogrp_rel = next((x for x in db.get_releases(include_ignored=True) if x["guid"] == "adv-nogroup-1"), None)
        r.check(nogrp_rel is not None and nogrp_rel["release_group"] == "",
                "Release without bracketed group tag stored with empty release_group string")

        # ---------------------------------------------------------------------
        # 1.5 Early Termination via Stop Check Flag
        # ---------------------------------------------------------------------
        print("\n--- 1.5 Early Termination via stop_backfill_requested ---")
        tm.stop_backfill_requested = True
        with patch("app.providers.nyaa.NyaaNovelProvider.search_series", return_value=mock_candidates):
            stopped_res = tm.backfill_series("Overlord", max_pages=1)
            r.check(stopped_res.get("releases_found") == 0,
                    f"Early stop flag terminates processing: found {stopped_res.get('releases_found')} items")
        tm.stop_backfill_requested = False

    finally:
        tm.stop()
        if hasattr(tm, "_cover_executor"):
            tm._cover_executor.shutdown(wait=False)
        shutil.rmtree(tmp_dir, ignore_errors=True)


# =============================================================================
# PART 2: FULL API SURFACE TESTING
# =============================================================================
def run_part_2_api_surface(r: ChallengerRecorder):
    print("\n" + "=" * 78)
    print("  PART 2: FULL API SURFACE TESTING & DUAL-ROUTE PARITY")
    print("=" * 78)

    # Use main app DB for API tests
    main_db = app.main.db
    main_db.init_db()
    main_db.ensure_prioritized_groups_table()

    # -------------------------------------------------------------------------
    # 2.1 Route Registration & Dual-Route Parity for Prioritized Groups
    # -------------------------------------------------------------------------
    print("\n--- 2.1 Dual-Route Parity: /api/prioritized_groups & /api/prioritized/groups ---")

    # A. Check route existence in registry
    r.check(("GET", "/api/prioritized_groups") in ROUTE_REGISTRY,
            "Route GET /api/prioritized_groups registered")
    r.check(("GET", "/api/prioritized/groups") in ROUTE_REGISTRY,
            "Route GET /api/prioritized/groups registered")
    r.check(("POST", "/api/prioritized_groups") in ROUTE_REGISTRY,
            "Route POST /api/prioritized_groups registered")
    r.check(("POST", "/api/prioritized/groups") in ROUTE_REGISTRY,
            "Route POST /api/prioritized/groups registered")
    r.check(("DELETE", "/api/prioritized_groups/{group_id}") in ROUTE_REGISTRY,
            "Route DELETE /api/prioritized_groups/{group_id} registered")
    r.check(("DELETE", "/api/prioritized/groups/{group_id}") in ROUTE_REGISTRY,
            "Route DELETE /api/prioritized/groups/{group_id} registered")
    r.check(("GET", "/api/prioritized/releases") in ROUTE_REGISTRY,
            "Route GET /api/prioritized/releases registered")

    handler_get_1 = ROUTE_REGISTRY[("GET", "/api/prioritized_groups")]
    handler_get_2 = ROUTE_REGISTRY[("GET", "/api/prioritized/groups")]
    r.check(handler_get_1 == handler_get_2, "Dual GET routes bind to identical handler function")

    handler_post_1 = ROUTE_REGISTRY[("POST", "/api/prioritized_groups")]
    handler_post_2 = ROUTE_REGISTRY[("POST", "/api/prioritized/groups")]
    r.check(handler_post_1 == handler_post_2, "Dual POST routes bind to identical handler function")

    handler_del_1 = ROUTE_REGISTRY[("DELETE", "/api/prioritized_groups/{group_id}")]
    handler_del_2 = ROUTE_REGISTRY[("DELETE", "/api/prioritized/groups/{group_id}")]
    r.check(handler_del_1 == handler_del_2, "Dual DELETE routes bind to identical handler function")

    # B. Test GET output parity
    res_get1 = run_async(handler_get_1())
    res_get2 = run_async(handler_get_2())
    data_get1 = get_response_data(res_get1)
    data_get2 = get_response_data(res_get2)

    r.check(data_get1.get("status") == "success", "GET /api/prioritized_groups returns status 'success'")
    r.check(data_get2.get("status") == "success", "GET /api/prioritized/groups returns status 'success'")
    r.check(len(data_get1.get("groups", [])) == len(data_get2.get("groups", [])),
            f"Dual GET endpoints return identical group count: {len(data_get1.get('groups', []))}")
    r.check(len(data_get1.get("groups", [])) >= 5, "Seeded default groups include >= 5 entries")

    # C. Test POST input validation & parity
    res_empty_name = run_async(handler_post_1({"name": ""}))
    r.check(res_empty_name.status_code == 400, f"Empty group name returns HTTP 400: {res_empty_name.status_code}")

    res_ws_name = run_async(handler_post_2({"name": "   "}))
    r.check(res_ws_name.status_code == 400, f"Whitespace group name returns HTTP 400: {res_ws_name.status_code}")

    # Add via Route 1
    res_add1 = run_async(handler_post_1({"name": "ChallengerGroupAlpha", "notes": "Route 1", "priority": 1}))
    r.check(res_add1.status_code == 200, "POST /api/prioritized_groups creates ChallengerGroupAlpha")
    r.check(main_db.is_group_prioritized("ChallengerGroupAlpha"), "ChallengerGroupAlpha is recognized in DB")

    # Add via Route 2
    res_add2 = run_async(handler_post_2({"name": "ChallengerGroupBeta", "notes": "Route 2", "priority": 2}))
    r.check(res_add2.status_code == 200, "POST /api/prioritized/groups creates ChallengerGroupBeta")
    r.check(main_db.is_group_prioritized("ChallengerGroupBeta"), "ChallengerGroupBeta is recognized in DB")

    # D. Test DELETE parity & 404 guard
    alpha_id = next(g["id"] for g in main_db.get_prioritized_groups() if g["name"].lower() == "challengergroupalpha")
    beta_id = next(g["id"] for g in main_db.get_prioritized_groups() if g["name"].lower() == "challengergroupbeta")

    # Delete non-existent ID
    threw_404 = False
    try:
        run_async(handler_del_1(999999))
    except HTTPException as e:
        if e.status_code == 404:
            threw_404 = True
    r.check(threw_404, "DELETE /api/prioritized_groups/999999 raises HTTP 404 for non-existent group")

    # Delete Alpha via Route 1
    res_del1 = run_async(handler_del_1(alpha_id))
    r.check(res_del1.status_code == 200, f"DELETE route 1 removed group #{alpha_id}")
    r.check(not main_db.is_group_prioritized("ChallengerGroupAlpha"), "ChallengerGroupAlpha no longer prioritized")

    # Delete Beta via Route 2
    res_del2 = run_async(handler_del_2(beta_id))
    r.check(res_del2.status_code == 200, f"DELETE route 2 removed group #{beta_id}")
    r.check(not main_db.is_group_prioritized("ChallengerGroupBeta"), "ChallengerGroupBeta no longer prioritized")

    # E. Test GET /api/prioritized/releases
    handler_prio_rels = ROUTE_REGISTRY[("GET", "/api/prioritized/releases")]
    res_prio_rels = run_async(handler_prio_rels())
    data_prio_rels = get_response_data(res_prio_rels)
    r.check(data_prio_rels.get("status") == "success", "/api/prioritized/releases returns status 'success'")
    r.check("releases" in data_prio_rels and "count" in data_prio_rels,
            "/api/prioritized/releases payload has 'releases' and 'count'")

    # -------------------------------------------------------------------------
    # 2.2 1-Click Ignore & Unignore API Surface
    # -------------------------------------------------------------------------
    print("\n--- 2.2 1-Click Ignore & Unignore Endpoints ---")

    r.check(("POST", "/api/releases/{release_id}/ignore") in ROUTE_REGISTRY,
            "Route POST /api/releases/{release_id}/ignore registered")
    r.check(("POST", "/api/releases/{release_id}/unignore") in ROUTE_REGISTRY,
            "Route POST /api/releases/{release_id}/unignore registered")

    handler_ignore = ROUTE_REGISTRY[("POST", "/api/releases/{release_id}/ignore")]
    handler_unignore = ROUTE_REGISTRY[("POST", "/api/releases/{release_id}/unignore")]

    # A. 404 Handling for non-existent release
    threw_ign_404 = False
    try:
        run_async(handler_ignore(999999))
    except HTTPException as e:
        if e.status_code == 404:
            threw_ign_404 = True
    r.check(threw_ign_404, "Ignore on non-existent release ID raises HTTP 404")

    threw_unign_404 = False
    try:
        run_async(handler_unignore(999999))
    except HTTPException as e:
        if e.status_code == 404:
            threw_unign_404 = True
    r.check(threw_unign_404, "Unignore on non-existent release ID raises HTTP 404")

    # B. State transition tests on real release
    _, test_rel_id = main_db.upsert_release({
        "guid": "challenger-ignore-test-1",
        "title": "[Yen Press] Ignore Me Vol 1",
        "series_name": "Ignore Test Series",
        "volume": 1.0,
        "status": "REQUESTED"
    })

    # Call Ignore
    res_ign = run_async(handler_ignore(test_rel_id))
    r.check(res_ign.status_code == 200, f"POST /api/releases/{test_rel_id}/ignore returned HTTP 200")
    rel_after_ign = main_db.get_release_by_id(test_rel_id)
    r.check(rel_after_ign["status"] == "IGNORED", f"Status updated to IGNORED (got {rel_after_ign['status']})")
    r.check(rel_after_ign["ignored"] == 1, f"ignored column flag is 1 (got {rel_after_ign['ignored']})")

    # Verify exclusion from default get_releases()
    def_releases = [x["id"] for x in main_db.get_releases(include_ignored=False)]
    r.check(test_rel_id not in def_releases, "Ignored release excluded from default get_releases()")

    # Verify inclusion when include_ignored=True
    all_releases = [x["id"] for x in main_db.get_releases(include_ignored=True)]
    r.check(test_rel_id in all_releases, "Ignored release included when include_ignored=True")

    # Call Unignore
    res_unign = run_async(handler_unignore(test_rel_id))
    r.check(res_unign.status_code == 200, f"POST /api/releases/{test_rel_id}/unignore returned HTTP 200")
    rel_after_unign = main_db.get_release_by_id(test_rel_id)
    r.check(rel_after_unign["status"] == "REQUESTED", f"Status restored to REQUESTED (got {rel_after_unign['status']})")
    r.check(rel_after_unign["ignored"] == 0, f"ignored column flag reset to 0 (got {rel_after_unign['ignored']})")

    # Activity log checks
    recent_logs = main_db.get_logs(limit=10)
    has_ign_log = any(log["event_type"] == "RELEASE_IGNORED" for log in recent_logs)
    has_rest_log = any(log["event_type"] == "RELEASE_RESTORED" for log in recent_logs)
    r.check(has_ign_log, "RELEASE_IGNORED recorded in activity logs")
    r.check(has_rest_log, "RELEASE_RESTORED recorded in activity logs")

    # -------------------------------------------------------------------------
    # 2.3 Series Backfill API Endpoints
    # -------------------------------------------------------------------------
    print("\n--- 2.3 Series Backfill Endpoints ---")

    r.check(("POST", "/api/series/{series_id}/backfill") in ROUTE_REGISTRY,
            "Route POST /api/series/{series_id}/backfill registered")
    r.check(("POST", "/api/series/backfill") in ROUTE_REGISTRY,
            "Route POST /api/series/backfill registered")

    handler_bf_id = ROUTE_REGISTRY[("POST", "/api/series/{series_id}/backfill")]
    handler_bf_name = ROUTE_REGISTRY[("POST", "/api/series/backfill")]

    # A. 404 for non-existent series ID
    threw_bf_404 = False
    try:
        run_async(handler_bf_id(999999))
    except HTTPException as e:
        if e.status_code == 404:
            threw_bf_404 = True
    r.check(threw_bf_404, "Backfill by series ID raises HTTP 404 for non-existent series")

    # B. Seed a series and backfill by ID
    with main_db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO series_tracking (name, is_monitored) VALUES ('DanMachi', 1);")
        conn.commit()
    danmachi_id = next(s["id"] for s in main_db.get_all_series() if s["name"].lower() == "danmachi")

    with patch.object(app.main.task_manager, "run_series_backfill_async", return_value={"status": "started", "message": "Backfill started"}):
        res_bf_id = run_async(handler_bf_id(danmachi_id, {"max_pages": 2}))
        data_bf_id = get_response_data(res_bf_id)
        r.check(data_bf_id.get("status") == "started", f"Backfill by series ID returns started: {data_bf_id}")

    # C. Backfill by name: empty name validation
    res_empty_bf = run_async(handler_bf_name({"series_name": ""}))
    r.check(res_empty_bf.status_code == 400, "Backfill by name with empty string returns HTTP 400")

    # D. Backfill by name: valid series name
    with patch.object(app.main.task_manager, "run_series_backfill_async", return_value={"status": "started", "message": "Backfill started"}):
        res_bf_name = run_async(handler_bf_name({"series_name": "Overlord", "max_pages": 3}))
        data_bf_name = get_response_data(res_bf_name)
        r.check(data_bf_name.get("status") == "started", f"Backfill by name returns started: {data_bf_name}")

    # -------------------------------------------------------------------------
    # 2.4 /prioritized UI View & Navbar Active State
    # -------------------------------------------------------------------------
    print("\n--- 2.4 /prioritized HTML UI Route & Template Integrity ---")

    r.check(("GET", "/prioritized") in ROUTE_REGISTRY, "Route GET /prioritized registered")
    handler_prio_view = ROUTE_REGISTRY[("GET", "/prioritized")]

    # Test direct execution of prioritized_view for 'series' mode
    req_mock = MockRequest() if not HAS_FASTAPI else Request({"type": "http", "method": "GET", "url": "/prioritized"})
    view_series_res = run_async(handler_prio_view(req_mock, view="series"))
    r.check(hasattr(view_series_res, "template_name") and view_series_res.template_name == "prioritized.html",
            "prioritized_view renders 'prioritized.html'")
    ctx_series = getattr(view_series_res, "context", {})
    r.check(ctx_series.get("active_page") == "prioritized", "Context has active_page='prioritized'")
    r.check(ctx_series.get("current_view") == "series", "Context has current_view='series'")
    r.check("prioritized_groups" in ctx_series, "Context contains 'prioritized_groups'")
    r.check("grouped_series" in ctx_series, "Context contains 'grouped_series'")

    # Test direct execution of prioritized_view for 'flat' mode
    view_flat_res = run_async(handler_prio_view(req_mock, view="flat"))
    ctx_flat = getattr(view_flat_res, "context", {})
    r.check(ctx_flat.get("current_view") == "flat", "Context has current_view='flat'")
    r.check("releases" in ctx_flat, "Context contains 'releases'")

    # Test template files on disk
    prio_tpl_path = os.path.join(BACKEND_DIR, "app", "templates", "prioritized.html")
    r.check(os.path.isfile(prio_tpl_path), "prioritized.html exists on disk")
    with open(prio_tpl_path, "r", encoding="utf-8") as f:
        prio_src = f.read()

    r.check('{% extends "base.html" %}' in prio_src, "prioritized.html extends base.html")
    r.check("Prioritized Releases & Groups" in prio_src, "prioritized.html contains header")
    r.check("addPrioritizedGroup" in prio_src, "prioritized.html contains addPrioritizedGroup handler")
    r.check("removePrioritizedGroup" in prio_src, "prioritized.html contains removePrioritizedGroup handler")
    r.check("Group by Series" in prio_src, "prioritized.html contains Group by Series switcher")
    r.check("All Releases (Flat)" in prio_src, "prioritized.html contains All Releases (Flat) switcher")

    # Navbar check in base.html
    base_tpl_path = os.path.join(BACKEND_DIR, "app", "templates", "base.html")
    r.check(os.path.isfile(base_tpl_path), "base.html exists on disk")
    with open(base_tpl_path, "r", encoding="utf-8") as f:
        base_src = f.read()

    r.check('href="/prioritized"' in base_src, "base.html contains navbar link to /prioritized")
    r.check("{% if active_page == 'prioritized' %}bg-slate-800 text-amber-400 border border-slate-700{% else %}" in base_src,
            "base.html navbar has active state conditional for 'prioritized'")
    r.check("ignoreReleaseGlobal" in base_src, "base.html declares global ignoreReleaseGlobal helper")
    r.check("unignoreReleaseGlobal" in base_src, "base.html declares global unignoreReleaseGlobal helper")
    r.check("backfillSeriesGlobal" in base_src, "base.html declares global backfillSeriesGlobal helper")

    # Partial template buttons check
    series_chunk_path = os.path.join(BACKEND_DIR, "app", "templates", "partials", "series_card_chunk.html")
    flat_chunk_path = os.path.join(BACKEND_DIR, "app", "templates", "partials", "flat_release_row_chunk.html")
    with open(series_chunk_path, "r", encoding="utf-8") as f:
        series_chunk_src = f.read()
    with open(flat_chunk_path, "r", encoding="utf-8") as f:
        flat_chunk_src = f.read()

    r.check("ignoreReleaseGlobal" in series_chunk_src or "unignoreReleaseGlobal" in series_chunk_src or "ignore" in series_chunk_src.lower(),
            "series_card_chunk.html has ignore actions")
    r.check("ignoreReleaseGlobal" in flat_chunk_src and "unignoreReleaseGlobal" in flat_chunk_src,
            "flat_release_row_chunk.html has ignore and unignore action buttons")


# =============================================================================
# PART 3: ZERO-REGRESSION COMPONENT VERIFICATION
# =============================================================================
def run_part_3_regressions(r: ChallengerRecorder):
    print("\n" + "=" * 78)
    print("  PART 3: ZERO-REGRESSION COMPONENT VERIFICATION")
    print("=" * 78)

    # -------------------------------------------------------------------------
    # 3.1 Calibre Omnibus Scanner
    # -------------------------------------------------------------------------
    print("\n--- 3.1 Calibre Omnibus Scanner (scanner.py) ---")

    cases_omnibus = [
        ("Overlord, Vol. 01-03 [Digital]", {1.0, 2.0, 3.0}),
        ("Mushoku Tensei Vol 1, 2, and 3", {1.0, 2.0, 3.0}),
        ("DanMachi Books 1, 2, & 3", {1.0, 2.0, 3.0}),
        ("Ascendance of a Bookworm Vol 1, 2, + 3", {1.0, 2.0, 3.0}),
        ("86 - Eighty Six - Vol 01", {1.0}),
        ("DanMachi Vol 19.5", {19.5}),
    ]

    for title, expected_vols in cases_omnibus:
        _, parsed_vols = parse_calibre_volumes_and_series(title)
        r.check(parsed_vols == expected_vols,
                f"Omnibus parsing: '{title}' -> {expected_vols}",
                f"Expected {expected_vols}, got {parsed_vols}")

    # -------------------------------------------------------------------------
    # 3.2 Downloader Selective Downloader & Oxford Comma Parsing
    # -------------------------------------------------------------------------
    print("\n--- 3.2 Selective Downloader & Oxford Comma Parsing (qbit.py) ---")

    cases_qbit = [
        ("DanMachi Vol 1, 2, and 3.epub", {1.0, 2.0, 3.0}),
        ("Overlord v1, 2, 3, and 4.epub", {1.0, 2.0, 3.0, 4.0}),
        ("Ascendance of a Bookworm Books 1, 2, & 3.epub", {1.0, 2.0, 3.0}),
        ("Slime Volume 1, 2, + 3.epub", {1.0, 2.0, 3.0}),
        ("Vol 04.5.epub", {4.5}),
    ]

    for fname, expected_vols in cases_qbit:
        _, vols = parse_torrent_filename_volumes(fname)
        r.check(vols == expected_vols,
                f"qbit filename parsing: '{fname}' -> {expected_vols}",
                f"Expected {expected_vols}, got {vols}")

    # Selective volume priority logic test
    mock_files = [
        {"index": 0, "name": "DanMachi Vol 1 & 2.epub", "priority": 1},
        {"index": 1, "name": "DanMachi Vol 1, 2, and 3.epub", "priority": 1},
        {"index": 2, "name": "DanMachi Vol 4.epub", "priority": 1},
    ]

    # User owns volumes 1 and 2, needs volume 3 and 4
    client = QBitClient()
    client.get_torrent_files = MagicMock(return_value=mock_files)
    client.set_file_priorities = MagicMock(return_value=True)

    sel_res = client.apply_selective_volume_download(
        torrent_hash="test-selective-hash",
        owned_volumes={1.0, 2.0},
        wanted_volumes=None
    )

    r.check(sel_res.get("status") == "filtered", f"Selective filter status is 'filtered' (got {sel_res.get('status')})")
    r.check(0 in sel_res.get("skipped_indices", []), "File 0 (Vol 1 & 2 fully owned) was skipped")
    r.check(1 in sel_res.get("kept_indices", []), "File 1 (Vol 1, 2, and 3 partially missing) was KEPT")
    r.check(2 in sel_res.get("kept_indices", []), "File 2 (Vol 4 unowned) was KEPT")

    # -------------------------------------------------------------------------
    # 3.3 SSRF Protection Central Guard
    # -------------------------------------------------------------------------
    print("\n--- 3.3 Centralized SSRF Guard (security.py) ---")

    ssrf_blocked = [
        "http://127.0.0.1/",
        "http://127.0.0.1:8080/secret",
        "http://localhost/",
        "http://0.0.0.0/",
        "http://10.0.0.1/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
        "file:///C:/Windows/win.ini",
        "gopher://127.0.0.1:70/",
    ]

    for blocked_url in ssrf_blocked:
        is_safe = is_safe_public_url(blocked_url)
        r.check(not is_safe, f"SSRF blocked unsafe URL: {blocked_url}")

    # Public destinations must be allowed
    with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, None, None, None, ("185.199.108.153", 0))]):
        r.check(is_safe_public_url("https://nyaa.si/"), "SSRF allows public HTTPS: https://nyaa.si/ (mock DNS)")
    r.check(is_safe_public_url("http://1.1.1.1/feed.xml"), "SSRF allows public IP: http://1.1.1.1/feed.xml")


# =============================================================================
# MAIN RUNNER
# =============================================================================
def main():
    print("=" * 78)
    print(" NOVELTRAX MILESTONE 4: ADVERSARIAL INTEGRATION & STRESS TEST HARNESS")
    print("=" * 78)
    start_time = time.time()
    r = ChallengerRecorder()

    try:
        run_part_1_backfill_stress(r)
        run_part_2_api_surface(r)
        run_part_3_regressions(r)
    except Exception as e:
        print(f"\n❌ UNHANDLED EXCEPTION IN TEST RUNNER: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        if hasattr(app.main, "task_manager"):
            app.main.task_manager.stop()
            if hasattr(app.main.task_manager, "_cover_executor"):
                app.main.task_manager._cover_executor.shutdown(wait=False)
        shutil.rmtree(TMP_RUN_DIR, ignore_errors=True)

    elapsed = time.time() - start_time
    print("\n" + "=" * 78)
    print("  CHALLENGER INTEGRATION TEST SUMMARY")
    print("=" * 78)
    print(f"  Total Checks Executed : {r.passed + r.failed}")
    print(f"  Passed                : {r.passed}")
    print(f"  Failed                : {r.failed}")
    print(f"  Execution Time        : {elapsed:.2f}s")
    print("=" * 78)

    if r.failed > 0:
        print(f"\n❌ VERDICT: REQUEST_CHANGES — {r.failed} check(s) failed.")
        sys.exit(1)
    else:
        print("\n✅ VERDICT: APPROVE — 100% of integration & stress checks passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
