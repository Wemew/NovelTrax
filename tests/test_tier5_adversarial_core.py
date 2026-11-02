#!/usr/bin/env python3
"""
NovelTrax Tier 5 Adversarial Core Test Suite (White-Box Verification)
Targets:
- backend/app/security.py
- backend/app/calibre/scanner.py
- backend/app/downloader/qbit.py
- backend/app/timezone_util.py
- backend/app/main.py

Adversarial Dimensions:
1. SSRF & Security: IPv6-mapped IPv4, unusual subnets, malformed scheme/port, DNS failure, path traversal, webhook reflection.
2. Calibre Scanner: omnibus parsing with extreme ranges, descending ranges, zero volumes, unicode dashes, delimiters, zero-lock concurrency.
3. Downloader: selective volume unchecking logic, Oxford comma boundary cases, malformed torrent hashes/magnets, cloud metadata IP destinations.
4. Timezone & Main: localization filters, DST transitions, CSP & security header invariants, API route error handling.

RUNNER COMPATIBILITY:
Calibre standalone Python interpreter (`calibre-debug.exe -e tests/test_tier5_adversarial_core.py`)
All checks are implemented using unstrippable assertions (will never be stripped under Python's -O / -O2 optimization).
"""

import os
import sys
import re
import json
import socket
import ipaddress
import base64
import time
import tempfile
import shutil
import threading
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional, List, Set, Dict, Tuple

# Setup import paths
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

# Core Module Imports
from tests.mock_calibre import create_mock_calibre_db, add_book_to_mock_calibre
from app.database import Database
from app.security import (
    is_safe_public_url,
    validate_downloader_destination,
    is_cloud_metadata_ip,
    is_ip_disallowed,
    sanitize_calibre_path,
)
from app.calibre.scanner import CalibreScanner, parse_calibre_volumes_and_series
from app.downloader.qbit import (
    QBitClient,
    parse_torrent_filename_volumes,
    ConnectionTestResult,
    SelectiveDownloadResult,
)
from app.timezone_util import get_app_timezone, format_localtime, format_localdate
from app.notifier.webhook import NotificationService


# ==============================================================================
# UNSTRIPPABLE ASSERTION ENGINE (Resistant to Calibre's -O / -O2 Flag)
# ==============================================================================

def check_true(condition: Any, msg: str = "Condition expected to be True"):
    if not bool(condition):
        raise AssertionError(msg)

def check_false(condition: Any, msg: str = "Condition expected to be False"):
    if bool(condition):
        raise AssertionError(msg)

def check_eq(actual: Any, expected: Any, msg: Optional[str] = None):
    if actual != expected:
        raise AssertionError(msg or f"Expected {expected!r}, but got {actual!r}")

def check_ne(actual: Any, expected: Any, msg: Optional[str] = None):
    if actual == expected:
        raise AssertionError(msg or f"Expected value != {expected!r}, but both are equal")

def check_in(item: Any, container: Any, msg: Optional[str] = None):
    if item not in container:
        raise AssertionError(msg or f"Expected {item!r} to be in {container!r}")

def check_not_in(item: Any, container: Any, msg: Optional[str] = None):
    if item in container:
        raise AssertionError(msg or f"Expected {item!r} not to be in {container!r}")

def check_raises(exception_cls, callable_func, *args, **kwargs):
    try:
        callable_func(*args, **kwargs)
    except exception_cls:
        return
    except Exception as e:
        raise AssertionError(f"Expected exception {exception_cls.__name__}, but caught {type(e).__name__}: {e}")
    raise AssertionError(f"Expected exception {exception_cls.__name__}, but no exception was raised")


class AdversarialTestRunner:
    def __init__(self):
        self.total = 0
        self.passed = 0
        self.failed = 0
        self.failures: List[Tuple[str, str]] = []

    def banner(self, title: str):
        print("\n" + "=" * 76)
        print(f"  {title.upper()}")
        print("=" * 76)

    def run_test(self, test_name: str, test_func):
        self.total += 1
        try:
            test_func()
            self.passed += 1
            print(f"  [PASS] {test_name}")
        except AssertionError as e:
            self.failed += 1
            self.failures.append((test_name, f"AssertionError: {e}"))
            print(f"  [FAIL] {test_name} -> {e}")
        except Exception as e:
            self.failed += 1
            self.failures.append((test_name, f"{type(e).__name__}: {e}"))
            print(f"  [ERROR] {test_name} -> {type(e).__name__}: {e}")

    def summary(self) -> bool:
        print("\n" + "=" * 76)
        print("  TIER 5 ADVERSARIAL CORE SUITE EXECUTION SUMMARY")
        print("=" * 76)
        print(f"  Total Adversarial Tests : {self.total}")
        print(f"  Passed                  : {self.passed}")
        print(f"  Failed                  : {self.failed}")
        rate = (self.passed / max(1, self.total)) * 100.0
        print(f"  Success Rate            : {rate:.1f}%")
        if self.failures:
            print("\n  Failures / Errors:")
            for name, err in self.failures:
                print(f"    - {name}: {err}")
        print("=" * 76)
        return self.failed == 0


runner = AdversarialTestRunner()


# ==============================================================================
# SECTION 1: SSRF & NETWORK SECURITY ADVERSARIAL HARDENING
# ==============================================================================

runner.banner("Section 1: SSRF & Network Security Adversarial Hardening")

def test_t5_sec_01_ipv6_mapped_loopback():
    ip = ipaddress.ip_address("::ffff:127.0.0.1")
    check_true(is_ip_disallowed(ip), "IPv6-mapped 127.0.0.1 must be disallowed")
    check_false(is_safe_public_url("http://[::ffff:127.0.0.1]/"), "URL with ::ffff:127.0.0.1 must be blocked")
    check_false(is_safe_public_url("http://[::ffff:127.0.0.1]:8080/secret"), "URL with ::ffff:127.0.0.1:port must be blocked")
runner.run_test("T5_SEC_01: IPv6-mapped IPv4 loopback (::ffff:127.0.0.1) strictly blocked", test_t5_sec_01_ipv6_mapped_loopback)

def test_t5_sec_02_ipv6_mapped_cloud_metadata():
    ip = ipaddress.ip_address("::ffff:169.254.169.254")
    check_true(is_ip_disallowed(ip), "IPv6-mapped metadata IP must be disallowed")
    check_true(is_cloud_metadata_ip(ip), "is_cloud_metadata_ip must detect IPv6-mapped metadata IP")
    check_false(is_safe_public_url("http://[::ffff:169.254.169.254]/latest/meta-data/"), "URL targeting mapped metadata IP blocked")
runner.run_test("T5_SEC_02: IPv6-mapped IPv4 cloud metadata (::ffff:169.254.169.254) strictly blocked", test_t5_sec_02_ipv6_mapped_cloud_metadata)

def test_t5_sec_03_ipv6_mapped_rfc1918():
    rfc1918_ips = ["::ffff:10.0.0.1", "::ffff:172.16.0.1", "::ffff:192.168.1.1"]
    for ip_s in rfc1918_ips:
        ip = ipaddress.ip_address(ip_s)
        check_true(is_ip_disallowed(ip), f"{ip_s} must be disallowed")
        check_false(is_safe_public_url(f"http://[{ip_s}]/admin"), f"http://[{ip_s}]/admin must be blocked")
runner.run_test("T5_SEC_03: IPv6-mapped IPv4 RFC 1918 private subnets strictly blocked", test_t5_sec_03_ipv6_mapped_rfc1918)

def test_t5_sec_04_ipv6_mapped_current_net():
    ip = ipaddress.ip_address("::ffff:0.0.0.0")
    check_true(is_ip_disallowed(ip), "::ffff:0.0.0.0 must be disallowed")
    check_false(is_safe_public_url("http://[::ffff:0.0.0.0]/"), "URL with ::ffff:0.0.0.0 must be blocked")
runner.run_test("T5_SEC_04: IPv6-mapped IPv4 0.0.0.0 strictly blocked", test_t5_sec_04_ipv6_mapped_current_net)

def test_t5_sec_05_ipv6_mapped_hex_loopback():
    ip = ipaddress.ip_address("::ffff:7f00:1")
    check_true(is_ip_disallowed(ip), "Hex IPv6-mapped loopback ::ffff:7f00:1 must be disallowed")
    check_false(is_safe_public_url("http://[::ffff:7f00:1]/"), "URL with ::ffff:7f00:1 must be blocked")
runner.run_test("T5_SEC_05: IPv6-mapped hex loopback (::ffff:7f00:1) strictly blocked", test_t5_sec_05_ipv6_mapped_hex_loopback)

def test_t5_sec_06_current_network_subnet():
    targets = ["0.0.0.0", "0.1.2.3", "0.255.255.255"]
    for t in targets:
        ip = ipaddress.ip_address(t)
        check_true(is_ip_disallowed(ip), f"{t} in 0.0.0.0/8 must be disallowed")
        check_false(is_safe_public_url(f"http://{t}/"), f"http://{t}/ must be blocked")
runner.run_test("T5_SEC_06: Current network subnet (0.0.0.0/8) strictly blocked", test_t5_sec_06_current_network_subnet)

def test_t5_sec_07_cgnat_shared_space():
    cgnat_ips = ["100.64.0.1", "100.100.50.25", "100.127.255.254"]
    for t in cgnat_ips:
        ip = ipaddress.ip_address(t)
        check_true(is_ip_disallowed(ip), f"{t} in CGNAT 100.64.0.0/10 must be disallowed")
        check_false(is_safe_public_url(f"http://{t}/service"), f"http://{t}/service must be blocked")
runner.run_test("T5_SEC_07: Shared Address Space / CGNAT (100.64.0.0/10) strictly blocked", test_t5_sec_07_cgnat_shared_space)

def test_t5_sec_08_documentation_subnets():
    doc_ips = ["192.0.2.1", "198.51.100.1", "203.0.113.1"]
    for t in doc_ips:
        ip = ipaddress.ip_address(t)
        check_true(is_ip_disallowed(ip), f"{t} documentation network must be disallowed")
        check_false(is_safe_public_url(f"http://{t}/"), f"http://{t}/ must be blocked")
runner.run_test("T5_SEC_08: Documentation subnets (TEST-NET-1, 2, 3) strictly blocked", test_t5_sec_08_documentation_subnets)

def test_t5_sec_09_benchmarking_subnet():
    bench_ips = ["198.18.0.1", "198.19.255.254"]
    for t in bench_ips:
        ip = ipaddress.ip_address(t)
        check_true(is_ip_disallowed(ip), f"{t} benchmarking 198.18.0.0/15 must be disallowed")
        check_false(is_safe_public_url(f"http://{t}/benchmark"), f"http://{t}/benchmark must be blocked")
runner.run_test("T5_SEC_09: Benchmarking subnet (RFC 2544 - 198.18.0.0/15) strictly blocked", test_t5_sec_09_benchmarking_subnet)

def test_t5_sec_10_ietf_protocol_assignments():
    ip = ipaddress.ip_address("192.0.0.1")
    check_true(is_ip_disallowed(ip), "192.0.0.1 (RFC 6890) must be disallowed")
    check_false(is_safe_public_url("http://192.0.0.1/"), "http://192.0.0.1/ must be blocked")
runner.run_test("T5_SEC_10: IETF Protocol Assignments (192.0.0.0/24) strictly blocked", test_t5_sec_10_ietf_protocol_assignments)

def test_t5_sec_11_multicast_ranges():
    multi_ips = ["224.0.0.1", "239.255.255.250", "ff02::1", "ff05::2"]
    for t in multi_ips:
        ip = ipaddress.ip_address(t)
        check_true(is_ip_disallowed(ip), f"Multicast {t} must be disallowed")
        url = f"http://[{t}]/" if ":" in t else f"http://{t}/"
        check_false(is_safe_public_url(url), f"{url} must be blocked")
runner.run_test("T5_SEC_11: Multicast IPv4 (224.0.0.0/4) & IPv6 (ff00::/8) strictly blocked", test_t5_sec_11_multicast_ranges)

def test_t5_sec_12_reserved_ipv4_and_broadcast():
    reserved_ips = ["240.0.0.1", "250.1.2.3", "255.255.255.255"]
    for t in reserved_ips:
        ip = ipaddress.ip_address(t)
        check_true(is_ip_disallowed(ip), f"Reserved/Broadcast {t} must be disallowed")
        check_false(is_safe_public_url(f"http://{t}/"), f"http://{t}/ must be blocked")
runner.run_test("T5_SEC_12: Reserved IPv4 (240.0.0.0/4) and Broadcast strictly blocked", test_t5_sec_12_reserved_ipv4_and_broadcast)

def test_t5_sec_13_ipv6_loopback_and_ula():
    ipv6_internal = ["::1", "::", "fc00::1", "fd12:3456::1", "fe80::1", "fe80::dead:beef"]
    for t in ipv6_internal:
        ip = ipaddress.ip_address(t)
        check_true(is_ip_disallowed(ip), f"IPv6 {t} must be disallowed")
        check_false(is_safe_public_url(f"http://[{t}]/"), f"http://[{t}]/ must be blocked")
runner.run_test("T5_SEC_13: IPv6 Loopback, Unspecified, ULA (fc00::/7) and Link-Local blocked", test_t5_sec_13_ipv6_loopback_and_ula)

def test_t5_sec_14_ipv6_special_prefixes():
    special_ipv6 = ["64:ff9b::127.0.0.1", "100::1", "2001:db8::1"]
    for t in special_ipv6:
        ip = ipaddress.ip_address(t)
        check_true(is_ip_disallowed(ip), f"Special prefix {t} must be disallowed")
        check_false(is_safe_public_url(f"http://[{t}]/"), f"http://[{t}]/ must be blocked")
runner.run_test("T5_SEC_14: IPv6 Translation (64:ff9b::/96) and Documentation (2001:db8::/32) blocked", test_t5_sec_14_ipv6_special_prefixes)

def test_t5_sec_15_disallowed_protocols():
    disallowed = [
        "ftp://mirror.example.com/test.epub",
        "file:///etc/passwd",
        "file:///C:/Windows/win.ini",
        "gopher://internal.lan:70/",
        "javascript:alert(document.cookie)",
        "data:text/html,<script>alert(1)</script>",
        "blob:https://example.com/uuid-blob",
        "ldap://localhost:389/dc=example",
    ]
    for url in disallowed:
        check_false(is_safe_public_url(url), f"Disallowed scheme '{url}' must return False")
runner.run_test("T5_SEC_15: Disallowed non-HTTP(S) protocols strictly blocked", test_t5_sec_15_disallowed_protocols)

def test_t5_sec_16_malformed_url_inputs():
    malformed = [
        "",
        "   ",
        None,
        12345,
        {"url": "http://example.com"},
        "http://",
        "https://",
        "http:///path/only",
        "https://:8080/no-host",
        "http://[invalid-ipv6]/",
        "http://[::1/unclosed-bracket",
    ]
    for m in malformed:
        check_false(is_safe_public_url(m), f"Malformed input {m!r} must return False cleanly")
runner.run_test("T5_SEC_16: Malformed URLs, missing host, and invalid types handled safely", test_t5_sec_16_malformed_url_inputs)

def test_t5_sec_17_credentials_in_url():
    check_false(is_safe_public_url("http://admin:secret@127.0.0.1:8080/"), "Creds with 127.0.0.1 must be blocked")
    check_false(is_safe_public_url("http://user:pass@169.254.169.254/"), "Creds with metadata IP must be blocked")
    check_false(is_safe_public_url("http://user:pass@localhost:8080/"), "Creds with localhost must be blocked")
runner.run_test("T5_SEC_17: Embedded credentials pointing to loopback/metadata strictly blocked", test_t5_sec_17_credentials_in_url)

def test_t5_sec_18_dns_failure_behavior():
    non_existent = "http://this-adversarial-domain-must-not-exist-noveltrax-test-999.invalid/"
    # socket.getaddrinfo will raise socket.gaierror, must be caught and return False
    check_false(is_safe_public_url(non_existent), "Unresolvable domain must return False without throwing uncaught error")
runner.run_test("T5_SEC_18: DNS resolution failure returns False gracefully without unhandled exception", test_t5_sec_18_dns_failure_behavior)

def test_t5_sec_19_downloader_dns_failure():
    is_valid, msg = validate_downloader_destination("unresolvable-downloader-host-xyz-test.invalid", scheme="http", resolve_dns=True)
    check_false(is_valid, "Unresolvable host must fail validation")
    check_true("dns resolution failed" in msg.lower() or "could not resolve" in msg.lower(), f"Expected DNS error message, got {msg}")
runner.run_test("T5_SEC_19: Downloader destination validator captures DNS failure cleanly", test_t5_sec_19_downloader_dns_failure)

def test_t5_sec_20_path_traversal_null_byte():
    check_raises(ValueError, sanitize_calibre_path, "C:/calibre/\x00/metadata.db")
    check_raises(ValueError, sanitize_calibre_path, "/calibre/lib\x00rary")
runner.run_test("T5_SEC_20: Path traversal null byte injection strictly rejected with ValueError", test_t5_sec_20_path_traversal_null_byte)

def test_t5_sec_21_path_traversal_forbidden_extensions():
    forbidden = [
        "C:/calibre/../../Windows/System32/cmd.exe",
        "C:/calibre/test.dll",
        "/var/noveltrax/../../etc/app.env",
        "/calibre/secret.key",
        "C:/certs/server.pem",
        "/etc/nginx/nginx.conf",
        "C:/calibre/setup.bat",
        "C:/calibre/script.ps1",
        "C:/calibre/debug.log",
    ]
    for p in forbidden:
        check_raises(ValueError, sanitize_calibre_path, p)
runner.run_test("T5_SEC_21: Path traversal targeting executable/credential files rejected", test_t5_sec_21_path_traversal_forbidden_extensions)

def test_t5_sec_22_path_traversal_normalization():
    cleaned = sanitize_calibre_path("C:\\Calibre\\Library\\..\\Library")
    check_not_in("..", cleaned, "Normalized path should eliminate '..'")
    check_true("Library" in cleaned, "Target directory preserved")
runner.run_test("T5_SEC_22: Legitimate library path with relative dots normalized safely", test_t5_sec_22_path_traversal_normalization)

def test_t5_sec_23_webhook_diagnostic_sanitization():
    ns = NotificationService()
    # Test blocked internal target
    res = ns.test_webhook(discord_url="http://127.0.0.1:8080/webhook", apprise_url="http://169.254.169.254/meta")
    check_eq(res["status"], "error", "Blocked webhooks must return status 'error'")
    # Ensure raw response bodies or HTML tracebacks are NOT leaked in message
    for sub in res["results"].values():
        check_false(sub["success"])
        msg_lower = sub["message"].lower()
        check_not_in("<html", msg_lower, "HTML body reflection must be prevented")
        check_not_in("traceback", msg_lower, "Internal tracebacks must not be exposed")
        check_true("ssrf" in msg_lower or "public" in msg_lower or "blocked" in msg_lower)
runner.run_test("T5_SEC_23: Webhook diagnostic sanitization prevents body reflection and leakage", test_t5_sec_23_webhook_diagnostic_sanitization)


# ==============================================================================
# SECTION 2: CALIBRE SCANNER & ZERO-LOCK CONCURRENCY ADVERSARIAL HARDENING
# ==============================================================================

runner.banner("Section 2: Calibre Scanner & Zero-Lock Concurrency Adversarial Hardening")

def test_t5_cal_01_extreme_valid_range():
    title = "Guin Saga Vol 1-100"
    series, vols = parse_calibre_volumes_and_series(title)
    check_eq(len(vols), 100, f"Expected 100 volumes, got {len(vols)}")
    check_true(1.0 in vols and 100.0 in vols, "Volumes 1.0 and 100.0 must be present")
runner.run_test("T5_CAL_01: Omnibus parsing with extreme valid range (Vol 1-100) extracts all 100 volumes", test_t5_cal_01_extreme_valid_range)

def test_t5_cal_02_range_delta_boundary():
    # Delta of 100: 1 to 101 -> (101 - 1) == 100 <= 100 -> valid
    title = "Massive Series Vol 1-101"
    series, vols = parse_calibre_volumes_and_series(title)
    check_eq(len(vols), 101, f"Expected 101 volumes for delta 100, got {len(vols)}")
runner.run_test("T5_CAL_02: Omnibus parsing delta boundary (Vol 1-101, delta=100) parsed fully", test_t5_cal_02_range_delta_boundary)

def test_t5_cal_03_massive_dos_range_rejected():
    # Delta of 4999 > 100 -> oversized range rejected, fallback extracts single Vol 1
    title = "Huge Series Vol 1-5000"
    series, vols = parse_calibre_volumes_and_series(title)
    check_eq(vols, {1.0}, "Oversized range > 100 delta must reject range expansion")
runner.run_test("T5_CAL_03: Massive DoS range (Vol 1-5000) rejects expansion, preventing memory exhaustion", test_t5_cal_03_massive_dos_range_rejected)

def test_t5_cal_04_range_above_2000_rejected():
    # Numbers > 2000 rejected to avoid year/catalog IDs matching as volume ranges
    title = "Catalog Vol 2001-2005"
    series, vols = parse_calibre_volumes_and_series(title)
    check_eq(len(vols), 0, "Ranges with values > 2000 must be rejected")
runner.run_test("T5_CAL_04: Omnibus range with values > 2000 rejected as non-volume identifiers", test_t5_cal_04_range_above_2000_rejected)

def test_t5_cal_05_bracketed_oversized_range():
    title = "Series [1-250]"
    series, vols = parse_calibre_volumes_and_series(title)
    check_eq(len(vols), 0, "Bracketed range with delta > 100 must be rejected")
runner.run_test("T5_CAL_05: Bracketed oversized range ([1-250]) rejected safely", test_t5_cal_05_bracketed_oversized_range)

def test_t5_cal_06_inverted_range_with_prefix():
    title = "Overlord Vol 3-1"
    series, vols = parse_calibre_volumes_and_series(title)
    check_eq(vols, {1.0, 2.0, 3.0}, "Inverted range with 'Vol' prefix must expand to {1.0, 2.0, 3.0}")
runner.run_test("T5_CAL_06: Inverted descending range with Vol prefix (Vol 3-1) expanded accurately", test_t5_cal_06_inverted_range_with_prefix)

def test_t5_cal_07_inverted_bracketed_with_prefix():
    title = "[Vol 5-2]"
    series, vols = parse_calibre_volumes_and_series(title, series_name="Overlord")
    check_eq(vols, {2.0, 3.0, 4.0, 5.0}, "Inverted bracketed with prefix must expand correctly")
runner.run_test("T5_CAL_07: Inverted bracketed range with Vol prefix ([Vol 5-2]) expanded accurately", test_t5_cal_07_inverted_bracketed_with_prefix)

def test_t5_cal_08_inverted_bare_range_rejected():
    # Bare descending range without Vol prefix should NOT be expanded (guards against scores like 'Match 10-2')
    title = "Novel 5-1"
    series, vols = parse_calibre_volumes_and_series(title)
    check_eq(vols, set(), "Bare descending range without volume prefix must not expand")
runner.run_test("T5_CAL_08: Bare descending range without volume prefix (Novel 5-1) NOT expanded", test_t5_cal_08_inverted_bare_range_rejected)

def test_t5_cal_09_volume_zero_series_index():
    # Calibre series_index can be 0.0 (prequel/side volume)
    series, vols = parse_calibre_volumes_and_series("Overlord Prequel", series_name="Overlord", series_index=0.0)
    check_in(0.0, vols, "Volume 0.0 from series_index must be recognized as owned")
runner.run_test("T5_CAL_09: Volume 0 support from Calibre series_index=0.0 recognized", test_t5_cal_09_volume_zero_series_index)

def test_t5_cal_10_volume_zero_title():
    series, vols = parse_calibre_volumes_and_series("Overlord Vol 0")
    check_in(0.0, vols, "Vol 0 in title must be recognized")
runner.run_test("T5_CAL_10: Explicit Volume 0 in book title (Vol 0) extracted cleanly", test_t5_cal_10_volume_zero_title)

def test_t5_cal_11_volume_zero_range():
    series, vols = parse_calibre_volumes_and_series("Overlord Vol 0-2")
    check_eq(vols, {0.0, 1.0, 2.0}, "Range Vol 0-2 must contain {0.0, 1.0, 2.0}")
runner.run_test("T5_CAL_11: Omnibus range starting at Volume 0 (Vol 0-2) includes 0.0, 1.0, and 2.0", test_t5_cal_11_volume_zero_range)

def test_t5_cal_12_volume_zero_bracketed():
    series, vols = parse_calibre_volumes_and_series("Overlord [0-3]")
    check_eq(vols, {0.0, 1.0, 2.0, 3.0}, "Bracketed [0-3] must contain {0.0, 1.0, 2.0, 3.0}")
runner.run_test("T5_CAL_12: Bracketed range starting at 0 ([0-3]) parsed cleanly", test_t5_cal_12_volume_zero_bracketed)

def test_t5_cal_13_unicode_en_dash():
    series, vols = parse_calibre_volumes_and_series("Overlord Vol 1\u20133")
    check_eq(vols, {1.0, 2.0, 3.0}, "Unicode en-dash (\\u2013) must parse as range")
runner.run_test("T5_CAL_13: Unicode En-dash delimiter (\u2013) parsed into volume set", test_t5_cal_13_unicode_en_dash)

def test_t5_cal_14_unicode_em_dash():
    series, vols = parse_calibre_volumes_and_series("Overlord Vol 1\u20143")
    check_eq(vols, {1.0, 2.0, 3.0}, "Unicode em-dash (\\u2014) must parse as range")
runner.run_test("T5_CAL_14: Unicode Em-dash delimiter (\u2014) parsed into volume set", test_t5_cal_14_unicode_em_dash)

def test_t5_cal_15_tilde_delimiter():
    series, vols = parse_calibre_volumes_and_series("Overlord Vol 1~3")
    check_eq(vols, {1.0, 2.0, 3.0}, "Tilde delimiter (~) must parse as range")
runner.run_test("T5_CAL_15: Wave dash / Tilde delimiter (~) parsed into volume set", test_t5_cal_15_tilde_delimiter)

def test_t5_cal_16_slash_delimiter():
    series, vols = parse_calibre_volumes_and_series("Overlord Vol 1/3")
    check_eq(vols, {1.0, 2.0, 3.0}, "Slash delimiter (/) must parse as range")
runner.run_test("T5_CAL_16: Slash delimiter (/) parsed into volume set", test_t5_cal_16_slash_delimiter)

def test_t5_cal_17_plus_conjunction():
    series, vols = parse_calibre_volumes_and_series("Overlord Vol 1 + 2")
    check_eq(vols, {1.0, 2.0}, "Plus conjunction must extract {1.0, 2.0}")
runner.run_test("T5_CAL_17: Non-standard plus delimiter (Vol 1 + 2) extracted as list", test_t5_cal_17_plus_conjunction)

def test_t5_cal_18_ampersand_conjunction():
    series, vols = parse_calibre_volumes_and_series("Overlord Vol 1 & 2")
    check_eq(vols, {1.0, 2.0}, "Ampersand conjunction must extract {1.0, 2.0}")
runner.run_test("T5_CAL_18: Non-standard ampersand delimiter (Vol 1 & 2) extracted as list", test_t5_cal_18_ampersand_conjunction)

def test_t5_cal_19_comma_list():
    series, vols = parse_calibre_volumes_and_series("Overlord Vol 1, 2, 3")
    check_eq(vols, {1.0, 2.0, 3.0}, "Comma list must extract {1.0, 2.0, 3.0}")
runner.run_test("T5_CAL_19: Comma-separated list (Vol 1, 2, 3) extracted as volume set", test_t5_cal_19_comma_list)

def test_t5_cal_20_oxford_comma():
    series, vols = parse_calibre_volumes_and_series("Overlord Vol 1, 2, and 3")
    check_eq(vols, {1.0, 2.0, 3.0}, "Oxford comma list must extract {1.0, 2.0, 3.0}")
runner.run_test("T5_CAL_20: Oxford comma list (Vol 1, 2, and 3) parsed accurately", test_t5_cal_20_oxford_comma)

def test_t5_cal_21_numbered_series_guard():
    # Numbered series like '86' must not have '86' mistaken for volume number
    series, vols = parse_calibre_volumes_and_series("86 - Vol 1", series_index=1.0)
    check_eq(series, "86", "Detected series must be '86'")
    check_eq(vols, {1.0}, "Owned volume must be {1.0}, not {86.0}")
runner.run_test("T5_CAL_21: Numbered series title ('86 - Vol 1') protects series name from false volume match", test_t5_cal_21_numbered_series_guard)

def test_t5_cal_22_numbered_title_mob_psycho():
    series, vols = parse_calibre_volumes_and_series("Mob Psycho 100 - Vol 1", series_index=1.0)
    check_eq(series, "Mob Psycho 100", "Detected series must be 'Mob Psycho 100'")
    check_eq(vols, {1.0}, "Owned volume must be {1.0}, not {100.0}")
runner.run_test("T5_CAL_22: Numbered series ('Mob Psycho 100 - Vol 1') isolates series digits from volume", test_t5_cal_22_numbered_title_mob_psycho)

def test_t5_cal_23_default_index_discarded_if_non_one():
    # When Calibre defaults series_index to 1.0, but title is 'Overlord Vol 2', 1.0 must be discarded
    series, vols = parse_calibre_volumes_and_series("Overlord Vol 2", series_name="Overlord", series_index=1.0)
    check_not_in(1.0, vols, "Default series_index 1.0 must be discarded when title explicitly says Vol 2")
    check_eq(vols, {2.0}, "Vols must only contain {2.0}")
runner.run_test("T5_CAL_23: Default series_index=1.0 safely discarded when title specifies non-1.0 volume", test_t5_cal_23_default_index_discarded_if_non_one)

def test_t5_cal_24_concurrent_zerolock_reading_under_writes():
    # Stress test: SQLite database written continuously in WAL mode by one thread,
    # while CalibreScanner concurrently executes zero-lock reads with mode=ro&immutable=1
    temp_dir = tempfile.mkdtemp(prefix="noveltrax_cal_concurrency_")
    try:
        cdb = os.path.join(temp_dir, "metadata.db")
        create_mock_calibre_db(cdb)
        scanner = CalibreScanner(temp_dir)

        stop_evt = threading.Event()
        writer_errors: List[Exception] = []
        reader_errors: List[Exception] = []
        read_iterations = [0]

        def writer_job():
            try:
                conn = sqlite3.connect(cdb, timeout=10.0)
                conn.execute("PRAGMA journal_mode = WAL;")
                cur = conn.cursor()
                for i in range(40):
                    cur.execute(
                        "INSERT INTO books (title, series_index, uuid) VALUES (?, ?, ?);",
                        (f"Live Concurrency Book {i}", 1.0, f"uuid-live-{i}")
                    )
                    conn.commit()
                    time.sleep(0.005)
                conn.close()
            except Exception as e:
                writer_errors.append(e)

        def reader_job():
            try:
                while not stop_evt.is_set():
                    summary = scanner.get_library_summary()
                    check_true(summary.get("available"), "Library summary must be available during live writes")
                    books = scanner.get_all_books()
                    series_map = scanner.get_series_volumes_map()
                    read_iterations[0] += 1
                    time.sleep(0.002)
            except Exception as e:
                reader_errors.append(e)

        w_thread = threading.Thread(target=writer_job)
        r_threads = [threading.Thread(target=reader_job) for _ in range(3)]

        for r in r_threads:
            r.start()
        w_thread.start()

        w_thread.join(timeout=10.0)
        stop_evt.set()
        for r in r_threads:
            r.join(timeout=5.0)

        check_eq(len(writer_errors), 0, f"Writer encountered errors: {writer_errors}")
        check_eq(len(reader_errors), 0, f"Readers encountered errors: {reader_errors}")
        check_true(read_iterations[0] > 10, f"Expected >10 concurrent read iterations, got {read_iterations[0]}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
runner.run_test("T5_CAL_24: Multi-threaded zero-lock SQLite concurrency stress test (readers vs live writes)", test_t5_cal_24_concurrent_zerolock_reading_under_writes)


# ==============================================================================
# SECTION 3: DOWNLOADER ENGINE & QUEUE SAFETY ADVERSARIAL HARDENING
# ==============================================================================

runner.banner("Section 3: Downloader Engine & Queue Safety Adversarial Hardening")

class MockQBitServerForSelective:
    """Lightweight in-memory emulator for qBittorrent file priority endpoints."""
    def __init__(self, files: List[Dict[str, Any]]):
        self.files = files
        self.priorities: Dict[int, int] = {idx: 1 for idx in range(len(files))}

    def get_files(self, torrent_hash: str) -> List[Dict[str, Any]]:
        return self.files

    def set_prio(self, torrent_hash: str, indices: List[int], priority: int) -> bool:
        for idx in indices:
            self.priorities[idx] = priority
        return True


def test_t5_qbt_01_selective_unchecking_owned_files():
    mock_files = [
        {"id": 0, "name": "[Group] DanMachi - Vol 01.epub"},
        {"id": 1, "name": "[Group] DanMachi - Vol 02.epub"},
        {"id": 2, "name": "[Group] DanMachi - Vol 03.epub"},
    ]
    server = MockQBitServerForSelective(mock_files)
    client = QBitClient(host="localhost", port=8080, enabled=True)
    # Patch client file operations
    client.get_torrent_files = server.get_files
    client.set_file_priorities = server.set_prio

    # User already owns Vols 1 and 2 in Calibre
    result = client.apply_selective_volume_download(
        "4a9b2c8d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b",
        owned_volumes=[1.0, 2.0],
        series_name="DanMachi"
    )

    check_eq(int(result), 2, "Expected 2 skipped files (Vols 1 and 2)")
    check_eq(server.priorities[0], 0, "File 0 (Vol 1) must be set to priority 0")
    check_eq(server.priorities[1], 0, "File 1 (Vol 2) must be set to priority 0")
    check_eq(server.priorities[2], 1, "File 2 (Vol 3) must remain priority 1")
    check_eq(result["status"], "filtered")
runner.run_test("T5_QBT_01: Selective volume unchecking in multi-file torrent skips already owned volumes", test_t5_qbt_01_selective_unchecking_owned_files)

def test_t5_qbt_02_selective_unchecking_wanted_set():
    mock_files = [
        {"id": 0, "name": "Overlord Vol 01.epub"},
        {"id": 1, "name": "Overlord Vol 02.epub"},
        {"id": 2, "name": "Overlord Vol 03.epub"},
    ]
    server = MockQBitServerForSelective(mock_files)
    client = QBitClient(host="localhost", port=8080, enabled=True)
    client.get_torrent_files = server.get_files
    client.set_file_priorities = server.set_prio

    # Only Vol 3 is wanted
    result = client.apply_selective_volume_download(
        "4a9b2c8d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b",
        wanted_volumes=[3.0],
        series_name="Overlord"
    )
    check_eq(int(result), 2)
    check_in(0, result["skipped_indices"])
    check_in(1, result["skipped_indices"])
    check_eq(result["kept_indices"], [2])
runner.run_test("T5_QBT_02: Selective volume unchecking respects wanted_volumes set strictly", test_t5_qbt_02_selective_unchecking_wanted_set)

def test_t5_qbt_03_omnibus_skipped_if_all_owned():
    # File is an omnibus containing Vols 1 and 2
    mock_files = [
        {"id": 0, "name": "Overlord Vol 1-2.epub"},
        {"id": 1, "name": "Overlord Vol 3.epub"},
    ]
    server = MockQBitServerForSelective(mock_files)
    client = QBitClient(host="localhost", port=8080, enabled=True)
    client.get_torrent_files = server.get_files
    client.set_file_priorities = server.set_prio

    # User owns {1.0, 2.0} -> omnibus {1.0, 2.0} is a subset of owned -> SKIPPED
    result = client.apply_selective_volume_download(
        "4a9b2c8d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b",
        owned_volumes=[1.0, 2.0],
        series_name="Overlord"
    )
    check_eq(int(result), 1, "Omnibus Vol 1-2 must be skipped when both are owned")
    check_eq(server.priorities[0], 0)
    check_eq(server.priorities[1], 1)
runner.run_test("T5_QBT_03: Omnibus file in torrent skipped IF AND ONLY IF all contained volumes are owned", test_t5_qbt_03_omnibus_skipped_if_all_owned)

def test_t5_qbt_04_omnibus_kept_if_any_missing():
    # File is an omnibus containing Vols 2 and 3
    mock_files = [
        {"id": 0, "name": "Overlord Vol 1.epub"},
        {"id": 1, "name": "Overlord Vol 2-3.epub"},
    ]
    server = MockQBitServerForSelective(mock_files)
    client = QBitClient(host="localhost", port=8080, enabled=True)
    client.get_torrent_files = server.get_files
    client.set_file_priorities = server.set_prio

    # User owns {1.0, 2.0}, but lacks {3.0} -> omnibus {2.0, 3.0} is NOT all owned -> KEPT
    result = client.apply_selective_volume_download(
        "4a9b2c8d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b",
        owned_volumes=[1.0, 2.0],
        series_name="Overlord"
    )
    check_eq(int(result), 1, "Only single Vol 1 should be skipped; omnibus Vol 2-3 must be kept")
    check_eq(server.priorities[0], 0)
    check_eq(server.priorities[1], 1, "Omnibus Vol 2-3 must remain priority 1")
runner.run_test("T5_QBT_04: Omnibus file in torrent KEPT if even a single contained volume is missing", test_t5_qbt_04_omnibus_kept_if_any_missing)

def test_t5_qbt_05_monolithic_archive_handled():
    mock_files = [{"id": 0, "name": "Overlord Complete Series.zip"}]
    server = MockQBitServerForSelective(mock_files)
    client = QBitClient(host="localhost", port=8080, enabled=True)
    client.get_torrent_files = server.get_files
    client.set_file_priorities = server.set_prio

    result = client.apply_selective_volume_download(
        "4a9b2c8d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b",
        owned_volumes=[1.0, 2.0, 3.0]
    )
    check_eq(result["status"], "single_archive")
    check_eq(int(result), 0, "Single archive cannot be selectively split")
runner.run_test("T5_QBT_05: Monolithic single-archive torrent recognized as non-splittable", test_t5_qbt_05_monolithic_archive_handled)

def test_t5_qbt_06_ancillary_files_skipped():
    ancillary = [
        "cover.jpg", "info.nfo", "checksum.sfv", "details.txt",
        "index.html", "metadata.xml", "banner.png"
    ]
    for fn in ancillary:
        is_anc, vols = parse_torrent_filename_volumes(fn)
        check_true(is_anc, f"File '{fn}' must be recognized as ancillary clutter")
        check_eq(vols, set())
runner.run_test("T5_QBT_06: Ancillary clutter files (.txt, .nfo, .jpg, .sfv) automatically identified", test_t5_qbt_06_ancillary_files_skipped)

def test_t5_qbt_07_result_coercion_helpers():
    sel_res = SelectiveDownloadResult({"status": "filtered", "skipped_files": 4})
    check_eq(int(sel_res), 4, "SelectiveDownloadResult must cast cleanly to int")
    check_true(sel_res == 4, "SelectiveDownloadResult must compare equal to int")

    conn_res = ConnectionTestResult({"success": True, "version": "v4.5.2"})
    check_true(bool(conn_res), "ConnectionTestResult must evaluate as True")
    check_true(conn_res == True, "ConnectionTestResult must compare equal to True")
runner.run_test("T5_QBT_07: ConnectionTestResult and SelectiveDownloadResult coercion invariants", test_t5_qbt_07_result_coercion_helpers)

def test_t5_qbt_08_torrent_oxford_comma():
    fn = "[Yen Press] Overlord Vol 1, 2, and 3.epub"
    is_anc, vols = parse_torrent_filename_volumes(fn, series_name="Overlord")
    check_false(is_anc)
    check_eq(vols, {1.0, 2.0, 3.0}, "Torrent filename Oxford comma must extract {1.0, 2.0, 3.0}")
runner.run_test("T5_QBT_08: Oxford comma in torrent filenames parsed into multiple volumes", test_t5_qbt_08_torrent_oxford_comma)

def test_t5_qbt_09_torrent_conjunction_lists():
    tests = [
        ("Series Vol 1 & 2.epub", {1.0, 2.0}),
        ("Series v1+v2.epub", {1.0, 2.0}),
        ("Series Book 1 and 2.epub", {1.0, 2.0}),
    ]
    for fn, expected in tests:
        is_anc, vols = parse_torrent_filename_volumes(fn, series_name="Series")
        check_false(is_anc)
        check_eq(vols, expected, f"Expected {expected} for {fn}")
runner.run_test("T5_QBT_09: Conjunction patterns (&, +, and) in torrent filenames parsed accurately", test_t5_qbt_09_torrent_conjunction_lists)

def test_t5_qbt_10_fractional_volume_range():
    fn = "Series Vol 1.5-3.epub"
    is_anc, vols = parse_torrent_filename_volumes(fn, series_name="Series")
    check_false(is_anc)
    check_eq(vols, {1.5, 2.0, 3.0}, "Fractional range Vol 1.5-3 must contain {1.5, 2.0, 3.0}")
    check_not_in(1.0, vols, "Integer 1.0 must not be present when range starts at 1.5")
runner.run_test("T5_QBT_10: Fractional volume range (Vol 1.5-3) parsed without spurious integer 1.0", test_t5_qbt_10_fractional_volume_range)

def test_t5_qbt_11_torrent_oversized_omnibus():
    fn = "Guin Saga Vol 1-120.epub"
    is_anc, vols = parse_torrent_filename_volumes(fn, series_name="Guin Saga")
    check_false(is_anc)
    check_eq(vols, set(), "Oversized range > 100 volumes returns empty set to safely keep file")
runner.run_test("T5_QBT_11: Oversized omnibus in torrent (>100 vols) kept safely without unchecking", test_t5_qbt_11_torrent_oversized_omnibus)

def test_t5_qbt_12_torrent_inverted_range():
    fn = "Series Vol 03-01.epub"
    is_anc, vols = parse_torrent_filename_volumes(fn, series_name="Series")
    check_false(is_anc)
    check_eq(vols, {1.0, 2.0, 3.0}, "Inverted range in torrent must parse to {1.0, 2.0, 3.0}")
runner.run_test("T5_QBT_12: Inverted descending range in torrent filename (Vol 03-01) parsed", test_t5_qbt_12_torrent_inverted_range)

def test_t5_qbt_13_tag_stripping_precision():
    fn = "[Group] [1080p] (Digital) [Yen Press] 86 - Vol 01.epub"
    is_anc, vols = parse_torrent_filename_volumes(fn, series_name="86")
    check_false(is_anc)
    check_eq(vols, {1.0}, "Multiple leading tags stripped cleanly; Vol 01 parsed")
runner.run_test("T5_QBT_13: Multiple nested/adjacent release group tags stripped before parsing", test_t5_qbt_13_tag_stripping_precision)

def test_t5_qbt_14_hash_extraction_hex():
    h40 = "4a9b2c8d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b"
    check_eq(QBitClient.extract_torrent_hash(h40), h40)
    check_eq(QBitClient.extract_torrent_hash(h40.upper()), h40)
runner.run_test("T5_QBT_14: Normalized 40-character hex hash extracted cleanly", test_t5_qbt_14_hash_extraction_hex)

def test_t5_qbt_15_hash_extraction_base32():
    # 20 bytes SHA1 in Base32 = 32 chars
    raw = bytes.fromhex("4a9b2c8d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b")
    b32 = base64.b32encode(raw).decode()
    check_eq(len(b32), 32)
    check_eq(QBitClient.extract_torrent_hash(b32), "4a9b2c8d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b")
runner.run_test("T5_QBT_15: Base32 (32-character) hash decoded to 40-char hex correctly", test_t5_qbt_15_hash_extraction_base32)

def test_t5_qbt_16_magnet_uri_40hex():
    h = "4a9b2c8d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b"
    magnet = f"magnet:?xt=urn:btih:{h.upper()}&dn=Overlord+Vol+19"
    check_eq(QBitClient.extract_torrent_hash(magnet), h)
runner.run_test("T5_QBT_16: Magnet URI with urn:btih: 40-hex hash extracted and lowercased", test_t5_qbt_16_magnet_uri_40hex)

def test_t5_qbt_17_magnet_uri_base32():
    raw = bytes.fromhex("4a9b2c8d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b")
    b32 = base64.b32encode(raw).decode()
    magnet = f"magnet:?xt=urn:btih:{b32}&dn=Overlord+Vol+19"
    check_eq(QBitClient.extract_torrent_hash(magnet), "4a9b2c8d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b")
runner.run_test("T5_QBT_17: Magnet URI with urn:btih: Base32 hash decoded to 40-char hex", test_t5_qbt_17_magnet_uri_base32)

def test_t5_qbt_18_url_embedded_torrent_hash():
    h = "4a9b2c8d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b"
    url = f"https://nyaa.si/download/{h}.torrent"
    check_eq(QBitClient.extract_torrent_hash(url), h)
runner.run_test("T5_QBT_18: Direct HTTP .torrent URL with embedded hash extracted cleanly", test_t5_qbt_18_url_embedded_torrent_hash)

def test_t5_qbt_19_malformed_hashes():
    malformed = [
        "",
        "   ",
        None,
        12345,
        "4a9b2c8d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6",   # 39 chars
        "4a9b2c8d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7", # 41 chars
        "4a9b2c8d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6z", # invalid hex (contains non-hex 'z')
        "magnet:?dn=TestWithoutHash",
        "magnet:?xt=urn:btih:INVALID_TOO_SHORT",
        "https://example.com/download/not_a_hash.epub",
        "https://example.com/torrents/index.html",
    ]
    for m in malformed:
        check_eq(QBitClient.extract_torrent_hash(m), None, f"Expected None for {m!r}")
runner.run_test("T5_QBT_19: Malformed hashes (invalid lengths, invalid characters) return None safely", test_t5_qbt_19_malformed_hashes)

def test_t5_qbt_20_downloader_blocks_cloud_metadata():
    targets = [
        "http://169.254.169.254:8080",
        "169.254.169.254:8080",
        "169.254.0.1",
        "http://[::ffff:169.254.169.254]:8080",
        "http://[fe80::1]:8080",
        "http://metadata.google.internal",
        "metadata.google.internal",
    ]
    for t in targets:
        valid, msg = validate_downloader_destination(t)
        check_false(valid, f"Downloader destination '{t}' must be blocked")
        check_true("cloud metadata" in msg.lower() or "disallowed" in msg.lower())
runner.run_test("T5_QBT_20: Cloud metadata IP destinations strictly blocked for downloader client", test_t5_qbt_20_downloader_blocks_cloud_metadata)

def test_t5_qbt_21_downloader_disallowed_schemes():
    bad_schemes = [
        "file:///downloads",
        "ftp://localhost:21",
        "gopher://localhost:70",
        "javascript:void(0)",
    ]
    for bs in bad_schemes:
        valid, msg = validate_downloader_destination(bs)
        check_false(valid, f"Scheme in '{bs}' must be disallowed")
runner.run_test("T5_QBT_21: Non-HTTP(S) downloader schemes strictly blocked", test_t5_qbt_21_downloader_disallowed_schemes)

def test_t5_qbt_22_downloader_permitted_destinations():
    permitted = [
        "localhost",
        "127.0.0.1",
        "http://127.0.0.1:8080",
        "[::1]:8080",
        "http://localhost:8080",
        "192.168.1.50:8080",
        "10.0.0.5",
        "172.16.0.2",
    ]
    for p in permitted:
        valid, msg = validate_downloader_destination(p, resolve_dns=False)
        check_true(valid, f"Expected permitted destination for '{p}', got error: {msg}")
runner.run_test("T5_QBT_22: Loopback and RFC 1918 LAN destinations permitted for downloader", test_t5_qbt_22_downloader_permitted_destinations)


# ==============================================================================
# SECTION 4: TIMEZONE & MAIN API ARCHITECTURE ADVERSARIAL HARDENING
# ==============================================================================

runner.banner("Section 4: Timezone & Main API Architecture Adversarial Hardening")

def test_t5_tm_01_custom_timezone_resolution():
    tz = get_app_timezone("America/New_York")
    check_eq(str(tz), "America/New_York")
    tz2 = get_app_timezone(custom_tz="Asia/Tokyo")
    check_eq(str(tz2), "Asia/Tokyo")
runner.run_test("T5_TM_01: Custom timezone resolution parameter strictly respected", test_t5_tm_01_custom_timezone_resolution)

def test_t5_tm_02_host_env_tz_resolution():
    old_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Pacific/Auckland"
        tz = get_app_timezone()
        check_eq(str(tz), "Pacific/Auckland")
    finally:
        if old_tz is not None:
            os.environ["TZ"] = old_tz
        else:
            os.environ.pop("TZ", None)
runner.run_test("T5_TM_02: Host environment TZ variable resolution verified", test_t5_tm_02_host_env_tz_resolution)

def test_t5_tm_03_invalid_timezone_fallback():
    # Corrupt/fictional zone name falls back gracefully without exception
    tz = get_app_timezone("Invalid_Fictional/Nowhere_Land")
    check_true(str(tz) in ("Europe/Copenhagen", "UTC") or "ZoneInfo" in str(type(tz)))
runner.run_test("T5_TM_03: Invalid or corrupted timezone falls back gracefully without crash", test_t5_tm_03_invalid_timezone_fallback)

def test_t5_tm_04_dst_winter_cet():
    # Danish winter: CET = UTC+1 (e.g. January 15)
    formatted = format_localtime("2026-01-15 12:00:00", tz_name="Europe/Copenhagen")
    check_eq(formatted, "2026-01-15 13:00:00", f"Expected CET (UTC+1) 13:00:00, got {formatted}")
runner.run_test("T5_TM_04: Daylight Saving Time winter transition (CET = UTC+1) verified", test_t5_tm_04_dst_winter_cet)

def test_t5_tm_05_dst_summer_cest():
    # Danish summer: CEST = UTC+2 (e.g. July 15)
    formatted = format_localtime("2026-07-15 12:00:00", tz_name="Europe/Copenhagen")
    check_eq(formatted, "2026-07-15 14:00:00", f"Expected CEST (UTC+2) 14:00:00, got {formatted}")
runner.run_test("T5_TM_05: Daylight Saving Time summer transition (CEST = UTC+2) verified", test_t5_tm_05_dst_summer_cest)

def test_t5_tm_06_date_only_formatting():
    formatted = format_localdate("2026-07-15 12:00:00", tz_name="Europe/Copenhagen")
    check_eq(formatted, "2026-07-15")
runner.run_test("T5_TM_06: Date-only formatting with format_localdate verified", test_t5_tm_06_date_only_formatting)

def test_t5_tm_07_timestamp_format_resilience():
    # ISO 8601 with trailing Z
    f1 = format_localtime("2026-01-15T12:00:00Z", tz_name="Europe/Copenhagen")
    check_eq(f1, "2026-01-15 13:00:00")

    # 16-character string YYYY-MM-DD HH:MM
    f2 = format_localtime("2026-01-15 12:00", tz_name="Europe/Copenhagen")
    check_eq(f2, "2026-01-15 13:00:00")

    # Date only string YYYY-MM-DD
    f3 = format_localtime("2026-01-15", tz_name="Europe/Copenhagen")
    check_eq(f3, "2026-01-15")

    # Microseconds string
    f4 = format_localtime("2026-01-15 12:00:00.999999", tz_name="Europe/Copenhagen")
    check_eq(f4, "2026-01-15 13:00:00")
runner.run_test("T5_TM_07: Timestamp string format resilience (ISO Z, short dates, microseconds)", test_t5_tm_07_timestamp_format_resilience)

def test_t5_tm_08_corrupt_date_handled():
    check_eq(format_localtime(None), "")
    check_eq(format_localtime(""), "")
    # Non-date string returned truncated/safe without exception
    check_eq(format_localtime("not-a-valid-date"), "not-a-valid-date")
runner.run_test("T5_TM_08: Corrupted/garbage timestamp inputs return safely without uncaught exception", test_t5_tm_08_corrupt_date_handled)

def test_t5_tm_09_csp_security_header_invariants():
    main_py_path = os.path.join(BACKEND_DIR, "app", "main.py")
    with open(main_py_path, "r", encoding="utf-8") as f:
        src = f.read()

    # Content-Security-Policy invariants
    check_true("default-src 'self'" in src, "CSP must include default-src 'self'")
    check_true("frame-ancestors 'self'" in src, "CSP must restrict frame-ancestors to 'self'")
    check_true("default-src *" not in src, "CSP must never allow wildcard default-src")
    check_true("script-src *" not in src, "CSP must never allow wildcard script-src")

    # Permissions-Policy invariants
    check_true("camera=()" in src, "Permissions-Policy must disable camera")
    check_true("microphone=()" in src, "Permissions-Policy must disable microphone")
    check_true("geolocation=()" in src, "Permissions-Policy must disable geolocation")

    # Standard security headers
    check_true("X-Content-Type-Options" in src and "nosniff" in src)
    check_true("X-Frame-Options" in src and "SAMEORIGIN" in src)
    check_true("X-XSS-Protection" in src)
    check_true("Referrer-Policy" in src)
runner.run_test("T5_TM_09: CSP and security header invariants in main.py verified", test_t5_tm_09_csp_security_header_invariants)

def test_t5_tm_10_template_xss_protection():
    templates_dir = os.path.join(BACKEND_DIR, "app", "templates")
    # Verify discovery.html escapes synopses
    disc_path = os.path.join(templates_dir, "discovery.html")
    with open(disc_path, "r", encoding="utf-8") as f:
        disc_src = f.read()
    check_true("replaceWith" in disc_src or "textContent" in disc_src or "escapeHtml" in disc_src)

    # Verify feeds.html escapes feed previews
    feeds_path = os.path.join(templates_dir, "feeds.html")
    with open(feeds_path, "r", encoding="utf-8") as f:
        feeds_src = f.read()
    check_true("escapeHtml" in feeds_src or "textContent" in feeds_src or "innerText" in feeds_src)
runner.run_test("T5_TM_10: Template XSS entity escaping and safe DOM properties verified", test_t5_tm_10_template_xss_protection)

def test_t5_tm_11_calibre_test_route_security_logic():
    # Simulate API route logic for /api/calibre/test
    def simulate_calibre_test_route(path_str):
        try:
            clean = sanitize_calibre_path(path_str)
            scanner = CalibreScanner(clean)
            return {"status_code": 200, "data": scanner.get_library_summary()}
        except ValueError as e:
            return {"status_code": 400, "error": str(e)}

    # Probing forbidden files
    res_exe = simulate_calibre_test_route("C:/Windows/cmd.exe")
    check_eq(res_exe["status_code"], 400)
    check_true("disallowed" in res_exe["error"].lower())

    res_null = simulate_calibre_test_route("C:/calibre/\x00/metadata.db")
    check_eq(res_null["status_code"], 400)
    check_true("null" in res_null["error"].lower())
runner.run_test("T5_TM_11: Calibre test route security logic rejects traversal and executable probes", test_t5_tm_11_calibre_test_route_security_logic)

def test_t5_tm_12_feed_test_route_ssrf_logic():
    # Simulate API route logic for /api/feeds/test
    def simulate_feed_test_route(url):
        if not url:
            return {"status_code": 400, "error": "URL is required"}
        if not is_safe_public_url(url):
            return {"status_code": 400, "error": "Blocked by SSRF protection"}
        return {"status_code": 200}

    check_eq(simulate_feed_test_route("http://127.0.0.1:8080/rss")["status_code"], 400)
    check_eq(simulate_feed_test_route("http://169.254.169.254/rss")["status_code"], 400)
    check_eq(simulate_feed_test_route("http://[::ffff:127.0.0.1]/rss")["status_code"], 400)
    check_eq(simulate_feed_test_route("")["status_code"], 400)
runner.run_test("T5_TM_12: Feed test route SSRF validation blocks loopback, metadata, and empty URLs", test_t5_tm_12_feed_test_route_ssrf_logic)

def test_t5_tm_13_downloader_configure_ssrf_logic():
    # Simulate API route logic for /api/downloader/configure
    def simulate_downloader_config(dest_url, use_ssl=False):
        scheme = "https" if use_ssl else "http"
        is_safe, error_msg = validate_downloader_destination(dest_url, scheme=scheme)
        if not is_safe:
            return {"status_code": 400, "error": error_msg}
        return {"status_code": 200, "message": "Saved"}

    check_eq(simulate_downloader_config("http://169.254.169.254:8080")["status_code"], 400)
    check_eq(simulate_downloader_config("http://metadata.google.internal")["status_code"], 400)
    check_eq(simulate_downloader_config("gopher://localhost:8080")["status_code"], 400)
    check_eq(simulate_downloader_config("http://127.0.0.1:8080")["status_code"], 200)
    check_eq(simulate_downloader_config("http://192.168.1.100:8080")["status_code"], 200)
runner.run_test("T5_TM_13: Downloader configuration route SSRF validation blocks cloud metadata", test_t5_tm_13_downloader_configure_ssrf_logic)

def test_t5_tm_14_release_ignore_restore_lifecycle():
    td = tempfile.mkdtemp(prefix="noveltrax_release_lifecycle_")
    try:
        app_db_path = os.path.join(td, "noveltrax.db")
        db = Database(app_db_path)

        # Upsert release
        rel = {
            "guid": "guid-lifecycle-test-1",
            "title": "Adversarial Life Novel Vol 1",
            "series_name": "Adversarial Life",
            "volume": 1.0,
            "status": "REQUESTED"
        }
        is_new, rel_id = db.upsert_release(rel)
        check_true(is_new)
        r = db.get_release_by_id(rel_id)
        check_true(r is not None)
        check_eq(r["status"], "REQUESTED")

        # Ignore
        db.ignore_release(rel_id)
        r_ignored = db.get_release_by_id(rel_id)
        check_eq(r_ignored["status"], "IGNORED")

        # Unignore / restore
        db.unignore_release(rel_id)
        r_restored = db.get_release_by_id(rel_id)
        check_eq(r_restored["status"], "REQUESTED")
    finally:
        shutil.rmtree(td, ignore_errors=True)
runner.run_test("T5_TM_14: Release 1-click ignore and restore state transitions verified", test_t5_tm_14_release_ignore_restore_lifecycle)

def test_t5_tm_15_database_settings_persistence():
    td = tempfile.mkdtemp(prefix="noveltrax_settings_")
    try:
        app_db_path = os.path.join(td, "noveltrax.db")
        db = Database(app_db_path)

        # Set and retrieve diverse setting types
        db.set_setting("qbit_enabled", "true")
        db.set_setting("qbit_port", "8080")
        db.set_setting("app_timezone", "Europe/Copenhagen")
        db.set_setting("custom_key", "custom_value_123")

        check_eq(db.get_setting("qbit_enabled"), "true")
        check_eq(db.get_setting("qbit_port"), "8080")
        check_eq(db.get_setting("app_timezone"), "Europe/Copenhagen")
        check_eq(db.get_setting("custom_key"), "custom_value_123")
        check_eq(db.get_setting("non_existent_key", "default_val"), "default_val")
    finally:
        shutil.rmtree(td, ignore_errors=True)
runner.run_test("T5_TM_15: Database settings persistence, update, and defaults verified", test_t5_tm_15_database_settings_persistence)

def test_t5_tm_16_database_prioritized_groups():
    td = tempfile.mkdtemp(prefix="noveltrax_prio_groups_")
    try:
        app_db_path = os.path.join(td, "noveltrax.db")
        db = Database(app_db_path)
        db.ensure_prioritized_groups_table()

        # Add groups
        db.add_prioritized_group("danke-Empire", notes="Retail VIP Group", priority=1)
        db.add_prioritized_group("Yen Press", notes="Official publisher", priority=2)

        groups = db.get_prioritized_groups()
        names = [g["name"] for g in groups]
        check_in("danke-Empire", names)
        check_in("Yen Press", names)

        # Remove group
        group_id = next(g["id"] for g in groups if g["name"] == "danke-Empire")
        removed = db.remove_prioritized_group(group_id)
        check_true(removed)

        remaining_names = [g["name"] for g in db.get_prioritized_groups()]
        check_not_in("danke-Empire", remaining_names)
        check_in("Yen Press", remaining_names)
    finally:
        shutil.rmtree(td, ignore_errors=True)
runner.run_test("T5_TM_16: Prioritized release groups database CRUD lifecycle verified", test_t5_tm_16_database_prioritized_groups)


# ==============================================================================
# MAIN EXECUTION ENTRY POINT
# ==============================================================================

if __name__ == "__main__":
    success = runner.summary()
    if not success:
        sys.exit(1)
    print("\n>>> ALL TIER 5 ADVERSARIAL CORE TESTS PASSED WITH 100% SUCCESS! <<<")
    sys.exit(0)
