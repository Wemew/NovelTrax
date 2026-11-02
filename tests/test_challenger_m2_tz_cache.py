"""
tests/test_challenger_m2_tz_cache.py

Empirical Challenger Stress Test Suite for NovelTrax Milestone 2:
- Timezone Daylight Saving Transitions (Europe/Copenhagen winter CET / summer CEST)
- Exact DST switch boundary verification (March & October 2026)
- Custom world timezones (America/New_York, Asia/Tokyo, UTC, invalid fallback)
- Malformed & edge-case timezone input handling (None, empty, partial, ISO formats, aware datetime)
- Multi-threaded SlidingWindowRateLimiter concurrency & rate-bounding invariant (25 threads)
- HTTP 429 Retry-After backoff & header handling
- SQLite WAL mode metadata cache concurrency under heavy multi-threaded read/write load
- Omnibus title parsing & search title cleaner adversarial fuzzing

Note: Calibre executes with -O2, so standard 'assert' statements may be stripped.
All verifications in this suite use explicit, non-strippable check() assertions.
"""

import os
import sys
import time
import shutil
import sqlite3
import tempfile
import threading
import zoneinfo
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Set

# Ensure backend directory is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.timezone_util import get_app_timezone, format_localtime, format_localdate
from app.providers.metadata import NovelMetadataProvider, SlidingWindowRateLimiter
from app.database import Database
from app.calibre.scanner import parse_calibre_volumes_and_series

def check(condition: bool, msg: str = ""):
    """Non-strippable assertion helper that survives Python -O/-O2 optimization."""
    if not condition:
        raise AssertionError(f"Check failed: {msg}")

def test_timezone_transitions_and_fuzzing():
    print("\n" + "=" * 70)
    print("  1. TIMEZONE DST TRANSITIONS & ADVERSARIAL INPUT FUZZING")
    print("=" * 70)
    passed = 0

    # 1a. Resolution & Default Fallback
    tz_default = get_app_timezone()
    check(tz_default.key == "Europe/Copenhagen", f"Expected 'Europe/Copenhagen', got {tz_default.key}")
    print("  ✓ get_app_timezone defaults to 'Europe/Copenhagen'")
    passed += 1

    # Custom parameter overrides
    tz_ny = get_app_timezone(custom_tz="America/New_York")
    check(tz_ny.key == "America/New_York", f"Expected 'America/New_York', got {tz_ny.key}")
    tz_tokyo = get_app_timezone(tz_name="Asia/Tokyo")
    check(tz_tokyo.key == "Asia/Tokyo", f"Expected 'Asia/Tokyo', got {tz_tokyo.key}")
    print("  ✓ Custom timezone parameters resolved accurately")
    passed += 1

    # Malformed / Unknown timezone fallbacks
    for bad_tz in ["Invalid/Timezone", "Mars/Olympus_Mons", "NotATimezone", ""]:
        fallback = get_app_timezone(bad_tz)
        check(fallback.key in ("Europe/Copenhagen", "UTC"), f"Expected safe fallback for '{bad_tz}', got {fallback.key}")
    print("  ✓ Malformed / unknown timezone names safely fall back without unhandled exceptions")
    passed += 1

    # 1b. Winter (CET, UTC+1) vs Summer (CEST, UTC+2)
    winter_utc = "2026-01-15 12:00:00"
    summer_utc = "2026-07-15 12:00:00"

    winter_local = format_localtime(winter_utc, tz_name="Europe/Copenhagen")
    check(winter_local == "2026-01-15 13:00:00", f"Winter expected 13:00:00 CET, got {winter_local}")
    print("  ✓ Winter UTC -> CET offset (+1 hour) verified: 12:00:00 -> 13:00:00")
    passed += 1

    summer_local = format_localtime(summer_utc, tz_name="Europe/Copenhagen")
    check(summer_local == "2026-07-15 14:00:00", f"Summer expected 14:00:00 CEST, got {summer_local}")
    print("  ✓ Summer UTC -> CEST offset (+2 hours) verified: 12:00:00 -> 14:00:00")
    passed += 1

    # format_localdate tests
    check(format_localdate(winter_utc) == "2026-01-15", "format_localdate winter date mismatch")
    check(format_localdate(summer_utc) == "2026-07-15", "format_localdate summer date mismatch")
    print("  ✓ format_localdate extracts exact localized YYYY-MM-DD")
    passed += 1

    # 1c. Exact DST Transition Boundaries (Europe/Copenhagen in 2026)
    # March 2026 DST switch: Last Sunday of March (2026-03-29) at 01:00:00 UTC
    # 00:59:59 UTC is CET (UTC+1) -> 01:59:59
    # 01:00:00 UTC is CEST (UTC+2) -> 03:00:00 (clocks jump forward 1 hour)
    m_before = format_localtime("2026-03-29 00:59:59", tz_name="Europe/Copenhagen")
    m_switch = format_localtime("2026-03-29 01:00:00", tz_name="Europe/Copenhagen")
    m_after  = format_localtime("2026-03-29 01:00:01", tz_name="Europe/Copenhagen")
    check(m_before == "2026-03-29 01:59:59", f"March before switch failed: {m_before}")
    check(m_switch == "2026-03-29 03:00:00", f"March exact switch failed: {m_switch}")
    check(m_after  == "2026-03-29 03:00:01", f"March after switch failed: {m_after}")
    print("  ✓ March 2026 DST transition boundary verified (01:59:59 CET -> 03:00:00 CEST)")
    passed += 1

    # October 2026 DST switch: Last Sunday of October (2026-10-25) at 01:00:00 UTC
    # 00:59:59 UTC is CEST (UTC+2) -> 02:59:59
    # 01:00:00 UTC is CET (UTC+1) -> 02:00:00 (clocks fall back 1 hour)
    o_before = format_localtime("2026-10-25 00:59:59", tz_name="Europe/Copenhagen")
    o_switch = format_localtime("2026-10-25 01:00:00", tz_name="Europe/Copenhagen")
    o_after  = format_localtime("2026-10-25 01:00:01", tz_name="Europe/Copenhagen")
    check(o_before == "2026-10-25 02:59:59", f"Oct before fallback failed: {o_before}")
    check(o_switch == "2026-10-25 02:00:00", f"Oct exact fallback failed: {o_switch}")
    check(o_after  == "2026-10-25 02:00:01", f"Oct after fallback failed: {o_after}")
    print("  ✓ October 2026 DST fallback boundary verified (02:59:59 CEST -> 02:00:00 CET)")
    passed += 1

    # 1d. Custom World Timezones
    # America/New_York (EST = UTC-5 in Jan, EDT = UTC-4 in Jul)
    ny_winter = format_localtime(winter_utc, tz_name="America/New_York")
    ny_summer = format_localtime(summer_utc, tz_name="America/New_York")
    check(ny_winter == "2026-01-15 07:00:00", f"Expected NY 07:00:00 EST, got {ny_winter}")
    check(ny_summer == "2026-07-15 08:00:00", f"Expected NY 08:00:00 EDT, got {ny_summer}")
    print("  ✓ America/New_York EST (UTC-5) and EDT (UTC-4) verified")
    passed += 1

    # Asia/Tokyo (JST = UTC+9 all year, no DST)
    tyo_winter = format_localtime(winter_utc, tz_name="Asia/Tokyo")
    tyo_summer = format_localtime(summer_utc, tz_name="Asia/Tokyo")
    check(tyo_winter == "2026-01-15 21:00:00", f"Expected Tokyo 21:00:00, got {tyo_winter}")
    check(tyo_summer == "2026-07-15 21:00:00", f"Expected Tokyo 21:00:00, got {tyo_summer}")
    print("  ✓ Asia/Tokyo fixed UTC+9 (no DST) verified")
    passed += 1

    # UTC
    utc_res = format_localtime(winter_utc, tz_name="UTC")
    check(utc_res == "2026-01-15 12:00:00", f"Expected UTC 12:00:00, got {utc_res}")
    print("  ✓ UTC (UTC+0) fixed conversion verified")
    passed += 1

    # 1e. Malformed, Degenerate & Edge-Case Timestamp Inputs
    check(format_localtime(None) == "", "format_localtime(None) should return empty string")
    check(format_localtime("") == "", "format_localtime('') should return empty string")
    # Non-empty unparseable string fallback produces str(value)[:19]
    check(format_localtime("   ") in ("", "   "), "format_localtime whitespace fallback")
    check(format_localdate(None) == "", "format_localdate(None) should return empty string")
    print("  ✓ None, empty string, and whitespace handled gracefully")
    passed += 1

    # ISO-8601 variations with 'T', 'Z', and fractional seconds
    iso_z = format_localtime("2026-07-15T12:00:00Z", tz_name="Europe/Copenhagen")
    check(iso_z == "2026-07-15 14:00:00", f"ISO with T and Z failed: {iso_z}")
    iso_frac = format_localtime("2026-07-15T12:00:00.123456Z", tz_name="Europe/Copenhagen")
    check(iso_frac == "2026-07-15 14:00:00", f"ISO with microseconds failed: {iso_frac}")
    iso_no_z = format_localtime("2026-07-15T12:00:00", tz_name="Europe/Copenhagen")
    check(iso_no_z == "2026-07-15 14:00:00", f"ISO without Z failed: {iso_no_z}")
    date_only = format_localtime("2026-07-15", tz_name="Europe/Copenhagen")
    check(date_only == "2026-07-15", f"Date only string failed: {date_only}")
    print("  ✓ ISO-8601 with 'T', 'Z', and fractional seconds formatted accurately")
    passed += 1

    # Datetime objects (naive and timezone-aware)
    dt_naive = datetime(2026, 7, 15, 12, 0, 0)
    res_naive = format_localtime(dt_naive, tz_name="Europe/Copenhagen")
    check(res_naive == "2026-07-15 14:00:00", f"Naive datetime failed: {res_naive}")

    dt_aware_utc = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    res_aware_utc = format_localtime(dt_aware_utc, tz_name="Europe/Copenhagen")
    check(res_aware_utc == "2026-07-15 14:00:00", f"Aware UTC datetime failed: {res_aware_utc}")

    dt_aware_ny = datetime(2026, 7, 15, 8, 0, 0, tzinfo=zoneinfo.ZoneInfo("America/New_York"))
    res_aware_ny = format_localtime(dt_aware_ny, tz_name="Europe/Copenhagen")
    check(res_aware_ny == "2026-07-15 14:00:00", f"Aware non-UTC datetime failed: {res_aware_ny}")
    print("  ✓ Datetime objects (naive UTC assumed, aware UTC, aware non-UTC) formatted accurately")
    passed += 1

    # Unparseable inputs (must not raise unhandled exception)
    bad_res1 = format_localtime("completely-invalid-date-string")
    check(bad_res1.startswith("completely-invalid"), f"Unparseable string fallback failed: {bad_res1}")
    bad_res2 = format_localtime(123456789)
    check(bad_res2 == "123456789", f"Integer input fallback failed: {bad_res2}")
    print("  ✓ Unparseable garbage strings and integer types survive with safe fallbacks")
    passed += 1

    return passed

def test_rate_limiter_concurrency_and_backoff():
    print("\n" + "=" * 70)
    print("  2. MULTI-THREADED RATE LIMITER CONCURRENCY & HTTP 429 BACKOFF")
    print("=" * 70)
    passed = 0

    # 2a. Multi-threaded Rate Limiter Concurrency & Exact Bounding
    # Setting max_requests=10 per 1.0s window
    limiter = SlidingWindowRateLimiter("ConcurrentTest", max_requests=10, time_window=1.0)
    completion_times: List[float] = []
    lock = threading.Lock()
    barrier = threading.Barrier(25)

    def worker():
        try:
            barrier.wait()
            limiter.wait_if_needed()
            with lock:
                completion_times.append(time.time())
        except Exception as e:
            print(f"Worker exception: {e}")

    threads = [threading.Thread(target=worker) for _ in range(25)]
    t_start = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    t_elapsed = time.time() - t_start

    check(len(completion_times) == 25, f"Expected 25 thread completions, got {len(completion_times)}")
    print(f"  ✓ 25 concurrent threads completed across SlidingWindowRateLimiter in {t_elapsed:.2f}s")
    passed += 1

    # Sliding window invariant check:
    # In any continuous window of length 0.99s, the number of successful requests cannot exceed 10.
    max_in_window = 0
    for t in completion_times:
        in_win = sum(1 for x in completion_times if (t - 0.99) <= x <= t)
        if in_win > max_in_window:
            max_in_window = in_win

    check(max_in_window <= 10, f"Rate limit violated! Max in 0.99s window: {max_in_window} > 10")
    print(f"  ✓ Strict sliding window invariant verified: max requests in any 0.99s window was {max_in_window} <= 10")
    passed += 1

    # Total elapsed time must be at least ~1.8s (since 25 requests with 10 req/s takes at least 2 full window resets)
    check(t_elapsed >= 1.8, f"Rate limiter was too fast! Completed in {t_elapsed:.2f}s, expected >= 1.8s")
    print(f"  ✓ Total pacing time verified: {t_elapsed:.2f}s >= 1.8s")
    passed += 1

    # 2b. HTTP 429 Cooldown & Retry-After Stress
    # Test valid float Retry-After
    test_limiter = SlidingWindowRateLimiter("429Test", max_requests=60, time_window=60.0)
    test_limiter.handle_rate_limit_error({"Retry-After": "0.5"})
    check(test_limiter.retry_until > time.time(), "retry_until should be set in the future")

    t_before = time.time()
    test_limiter.wait_if_needed()
    t_after = time.time()
    cooldown_waited = t_after - t_before
    # Note: wait_secs is max(1.0, float(retry_after)), so it waits >= 0.95s
    check(cooldown_waited >= 0.95, f"Expected cooldown wait >= 0.95s, got {cooldown_waited:.2f}s")
    print(f"  ✓ HTTP 429 Retry-After: 0.5 enforced cooldown ({cooldown_waited:.2f}s pause)")
    passed += 1

    # Test case-insensitive lowercase 'retry-after'
    test_limiter.handle_rate_limit_error({"retry-after": "1.2"})
    check(test_limiter.retry_until >= time.time() + 1.1, "lowercase retry-after not recognized")
    print("  ✓ Lowercase 'retry-after' header handled correctly")
    passed += 1

    # Test malformed / corrupt Retry-After values
    test_limiter.handle_rate_limit_error({"Retry-After": "corrupted_non_numeric"})
    check(test_limiter.retry_until > time.time(), "corrupted Retry-After should fallback to 60.0s")
    test_limiter.handle_rate_limit_error(None)
    check(test_limiter.retry_until > time.time(), "None header should fallback to 60.0s")
    print("  ✓ Corrupt/missing Retry-After values safely defaulted without unhandled exceptions")
    passed += 1

    # 2c. Near-quota pacing on X-RateLimit-Remaining
    # When remaining <= 2, pacing pause should be triggered
    pace_limiter = SlidingWindowRateLimiter("PaceTest", max_requests=60, time_window=60.0)
    t0 = time.time()
    pace_limiter.handle_response_headers({"X-RateLimit-Remaining": "1"})
    t1 = time.time()
    check((t1 - t0) >= 0.95, f"Expected near-quota pacing pause >= 0.95s, got {t1 - t0:.2f}s")
    print("  ✓ X-RateLimit-Remaining <= 2 triggers dynamic pacing delay")
    passed += 1

    # When remaining > 2, no pacing delay should occur
    t0 = time.time()
    pace_limiter.handle_response_headers({"X-RateLimit-Remaining": "50"})
    t1 = time.time()
    check((t1 - t0) < 0.2, f"Expected no pacing pause for high quota, took {t1 - t0:.2f}s")
    print("  ✓ High quota (remaining=50) executes with zero unnecessary delay")
    passed += 1

    return passed

def test_sqlite_wal_cache_concurrency():
    print("\n" + "=" * 70)
    print("  3. SQLITE WAL MODE METADATA CACHE CONCURRENCY STRESS")
    print("=" * 70)
    passed = 0

    tmp_dir = tempfile.mkdtemp(prefix="noveltrax_wal_stress_")
    try:
        db_path = os.path.join(tmp_dir, "wal_cache_stress.db")
        db = Database(db_path)
        db.ensure_metadata_cache_table()

        # Verify PRAGMA journal_mode is WAL
        with db.get_connection() as conn:
            mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            check(mode.upper() == "WAL", f"Expected WAL journal mode, got {mode}")
        print("  ✓ SQLite database confirmed running in WAL journal mode")
        passed += 1

        # 3a. Multi-threaded Reader/Writer Stress (25 concurrent threads)
        # 10 writers continuously upserting metadata records
        # 10 readers continuously retrieving records and asserting validity
        # 5 deleters/listers checking cache consistency
        errors: List[str] = []
        stop_event = threading.Event()
        barrier = threading.Barrier(25)

        def writer_task(tid: int):
            try:
                barrier.wait()
                for i in range(20):
                    if stop_event.is_set():
                        break
                    key = f"series_{tid}_{i % 5}"
                    data = {
                        "title": f"Series Title {tid} - {i}",
                        "synopsis": f"Synopsis content for worker {tid} iteration {i}",
                        "cover_url": f"https://example.com/cover_{tid}_{i}.jpg",
                        "source": "AniList"
                    }
                    db.cache_novel_metadata(key, data)
                    time.sleep(0.005)
            except Exception as e:
                errors.append(f"Writer {tid} error: {e}")

        def reader_task(tid: int):
            try:
                barrier.wait()
                for i in range(25):
                    if stop_event.is_set():
                        break
                    target_tid = tid % 10
                    key = f"series_{target_tid}_{i % 5}"
                    res = db.get_cached_novel_metadata(key)
                    if res is not None:
                        if not res.get("title") or not res.get("synopsis"):
                            errors.append(f"Corrupt record found by reader {tid} for {key}: {res}")
                    time.sleep(0.003)
            except Exception as e:
                errors.append(f"Reader {tid} error: {e}")

        def lister_task(tid: int):
            try:
                barrier.wait()
                for _ in range(10):
                    if stop_event.is_set():
                        break
                    all_c = db.get_all_novel_metadata_cache()
                    check(isinstance(all_c, dict), "get_all_novel_metadata_cache must return a dict")
                    time.sleep(0.01)
            except Exception as e:
                errors.append(f"Lister {tid} error: {e}")

        threads: List[threading.Thread] = []
        for i in range(10):
            threads.append(threading.Thread(target=writer_task, args=(i,)))
        for i in range(10):
            threads.append(threading.Thread(target=reader_task, args=(i,)))
        for i in range(5):
            threads.append(threading.Thread(target=lister_task, args=(i,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)

        check(len(errors) == 0, f"Concurrent WAL cache errors encountered: {errors}")
        print("  ✓ 25 concurrent reader/writer threads completed without database locked errors or corruption")
        passed += 1

        # 3b. Data integrity after concurrency
        final_cache = db.get_all_novel_metadata_cache()
        check(len(final_cache) > 0, "Final cache should contain records")
        print(f"  ✓ Cache integrity verified: {len(final_cache)} unique novel metadata keys stored")
        passed += 1

        # 3c. Case-insensitivity and update behavior
        db.cache_novel_metadata("Solo Leveling", {
            "title": "Solo Leveling",
            "synopsis": "Original synopsis",
            "cover_url": "https://example.com/solo.jpg",
            "source": "Kitsu"
        })
        # Retrieve with lowercase
        ret1 = db.get_cached_novel_metadata("solo leveling")
        check(ret1 is not None and ret1["title"] == "Solo Leveling", "Case-insensitive lookup failed")

        # Overwrite with uppercase
        db.cache_novel_metadata("SOLO LEVELING", {
            "title": "Solo Leveling (Updated)",
            "synopsis": "Updated synopsis",
            "cover_url": "https://example.com/solo_new.jpg",
            "source": "AniList"
        })
        ret2 = db.get_cached_novel_metadata("sOlO lEvElInG")
        check(ret2 is not None and ret2["title"] == "Solo Leveling (Updated)", "Update/overwrite on duplicate key failed")
        print("  ✓ Case-insensitive lookups and UPSERT updates verified")
        passed += 1

        # 3d. Clear cache
        deleted_count = db.clear_novel_metadata_cache()
        check(deleted_count >= 1, "clear_novel_metadata_cache returned 0")
        check(len(db.get_all_novel_metadata_cache()) == 0, "Cache should be empty after clear")
        print("  ✓ clear_novel_metadata_cache purged all entries cleanly")
        passed += 1

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return passed

def test_omnibus_and_title_cleaner_adversarial():
    print("\n" + "=" * 70)
    print("  4. OMNIBUS PARSER & METADATA TITLE CLEANER ADVERSARIAL STRESS")
    print("=" * 70)
    passed = 0

    # 4a. Omnibus bundle expansions
    # Reversed range (Vol 3-1 -> {1.0, 2.0, 3.0})
    _, vols_rev = parse_calibre_volumes_and_series("Overlord Vol 3-1", "Overlord", 1.0)
    check(vols_rev == {1.0, 2.0, 3.0}, f"Reversed range failed: {vols_rev}")
    print("  ✓ Reversed volume range ('Vol 3-1') expanded to {1.0, 2.0, 3.0}")
    passed += 1

    # Tilde range (Vol 1~3)
    _, vols_tilde = parse_calibre_volumes_and_series("Can't Fear Your Own World Vol 1~3", None, 1.0)
    check(vols_tilde == {1.0, 2.0, 3.0}, f"Tilde range failed: {vols_tilde}")
    print("  ✓ Tilde volume range ('Vol 1~3') expanded to {1.0, 2.0, 3.0}")
    passed += 1

    # Bracketed multi-word range '[Volume 1 to 4]'
    _, vols_brack = parse_calibre_volumes_and_series("DanMachi [Volume 1 to 4]", "DanMachi", 1.0)
    check(vols_brack == {1.0, 2.0, 3.0, 4.0}, f"Bracketed 'Volume 1 to 4' failed: {vols_brack}")
    print("  ✓ Bracketed 'Volume 1 to 4' expanded to {1.0, 2.0, 3.0, 4.0}")
    passed += 1

    # Large range clamp check (>100 volumes range should NOT expand to prevent memory exhaustion)
    _, vols_clamp = parse_calibre_volumes_and_series("Giant Collection Vol 1-500", "Giant Collection", 1.0)
    check(len(vols_clamp) <= 1, f"Expected range clamp to ignore >100 range, got {len(vols_clamp)} items")
    print("  ✓ Safety clamp: huge range ('Vol 1-500' > 100) ignored to prevent resource exhaustion")
    passed += 1

    # Decimal volume ('Vol 11.5')
    _, vols_dec = parse_calibre_volumes_and_series("86 Vol 11.5", "86", 1.0)
    check(vols_dec == {11.5}, f"Decimal volume failed: {vols_dec}")
    print("  ✓ Decimal volume ('Vol 11.5') extracted accurately as {11.5}")
    passed += 1

    # Malformed / empty / None inputs
    s_none, vols_none = parse_calibre_volumes_and_series("", None, None)
    check(vols_none == set() and s_none is None, "Empty title should return empty set and None")
    s_none2, vols_none2 = parse_calibre_volumes_and_series(None, "Series", 2.0)
    check(vols_none2 == {2.0} and s_none2 == "Series", "None title with series_index should retain index")
    print("  ✓ Empty and None inputs handled safely")
    passed += 1

    # 4b. NovelMetadataProvider Title Cleaners
    # clean_search_title
    c1 = NovelMetadataProvider.clean_search_title("DanMachi Volume 19 (Epub) [Light Novel]")
    check(c1 == "DanMachi", f"clean_search_title failed: '{c1}'")

    c2 = NovelMetadataProvider.clean_search_title("Re:Zero - Starting Life in Another World Vol 1-14 All Volumes")
    check("Re:Zero" in c2, f"clean_search_title failed: '{c2}'")

    c3 = NovelMetadataProvider.clean_search_title("")
    check(c3 == "", "clean_search_title empty string failed")

    c4 = NovelMetadataProvider.clean_search_title(None)
    check(c4 == "", "clean_search_title None failed")
    print("  ✓ clean_search_title strips format noise, ranges, and volume tags while preserving canonical titles")
    passed += 1

    # clean_novel_title
    n1 = NovelMetadataProvider.clean_novel_title("Sword Art Online (Light Novel)")
    check(n1 == "Sword Art Online", f"clean_novel_title failed: '{n1}'")
    n2 = NovelMetadataProvider.clean_novel_title("Overlord [Web Novel]")
    check(n2 == "Overlord", f"clean_novel_title failed: '{n2}'")
    print("  ✓ clean_novel_title strips format tags and bracketed suffixes")
    passed += 1

    return passed

def run_challenger_suite():
    print("=" * 70)
    print("  RUNNING EMPIRICAL CHALLENGER STRESS SUITE (MILESTONE 2)")
    print("=" * 70)
    total_passed = 0

    total_passed += test_timezone_transitions_and_fuzzing()
    total_passed += test_rate_limiter_concurrency_and_backoff()
    total_passed += test_sqlite_wal_cache_concurrency()
    total_passed += test_omnibus_and_title_cleaner_adversarial()

    print("\n" + "=" * 70)
    print(f"  ALL {total_passed} EMPIRICAL CHALLENGER ASSERTIONS PASSED WITH 100% SUCCESS!")
    print("=" * 70)

if __name__ == "__main__":
    run_challenger_suite()
