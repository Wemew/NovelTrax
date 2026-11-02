"""
tests/test_security_audit.py
Dedicated Security Hardening & Dependency Verification Test Suite for Milestone 1.

Verifies:
1. SSRF URL validation across all IPv4/IPv6 private, loopback, CGNAT, and cloud metadata ranges.
2. safe_http_get SSRF rejection and safe redirect hop validation.
3. LFI (Local File Inclusion) prevention in RSS feed fetching.
4. Webhook SSRF validation and response body sanitization.
5. Calibre path traversal and sensitive file probing prevention.
6. DOM XSS sanitization in discovery.html and feeds.html.
7. Dependency manifest minimization (requirements.txt).
8. J-Novels master index indentation defect remediation.
"""

import sys
import os
import re
import socket
from unittest.mock import patch, MagicMock

# Add base directory and backend directory to path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

from app.security import (
    is_safe_public_url,
    safe_http_get,
    safe_fetch_text,
    safe_client_get,
    sanitize_calibre_path,
    DISALLOWED_NETWORKS
)
from app.notifier.webhook import NotificationService
from app.providers.rss import RSSFeedProvider
from app.providers.base import ReleaseItem
import app.providers.jnovels as jnovels_mod
from app.providers.jnovels import JNovelsProvider
from bs4 import BeautifulSoup


def check(condition: bool, message: str = "") -> None:
    """
    Explicit runtime assertion check that is NEVER stripped under Calibre's -O2 compiler.
    Raises AssertionError when condition evaluates to False.
    """
    if not condition:
        raise AssertionError(message or "Assertion check failed")


def test_ssrf_disallowed_urls():
    print("\n--- Testing SSRF URL Validation ---")
    unsafe_urls = [
        # Loopback
        "http://127.0.0.1/",
        "http://127.0.0.1:8080/secret",
        "http://127.1.2.3/",
        "http://localhost/",
        "http://localhost:3000/",
        "http://0.0.0.0/",
        # Private IPv4 (RFC 1918)
        "http://10.0.0.1/",
        "http://10.255.255.254/",
        "http://172.16.0.1/",
        "http://172.31.255.255/",
        "http://192.168.0.1/",
        "http://192.168.1.254/",
        # Carrier-Grade NAT (RFC 6598 - 100.64.0.0/10)
        "http://100.64.0.1/",
        "http://100.100.50.1/",
        "http://100.127.255.254/",
        # Cloud Metadata & Link-Local (RFC 3927)
        "http://169.254.169.254/latest/meta-data/",
        "http://169.254.1.1/",
        "http://metadata.google.internal/computeMetadata/v1/",
        # IPv6 Loopback, ULA, Link-Local
        "http://[::1]/",
        "http://[fc00::1]/",
        "http://[fd12:3456:789a::1]/",
        "http://[fe80::1]/",
        # Non-HTTP protocols
        "file:///etc/passwd",
        "file:///C:/Windows/win.ini",
        "ftp://internal.lan/backup.zip",
        "gopher://127.0.0.1:70/",
        "javascript:alert(document.cookie)",
        # Malformed / Empty
        "",
        "   ",
        "not_a_url",
        "://missing-scheme"
    ]

    for url in unsafe_urls:
        check(not is_safe_public_url(url), f"Security Failure: Unsafe URL was not blocked: {url}")
        print(f"  ✓ Successfully blocked SSRF vector: {url}")

    # Verify public IP literals pass validation without DNS resolution requirement (100% offline resilience)
    safe_ip_urls = [
        "http://1.1.1.1/feed.xml",
        "https://8.8.8.8/path",
        "http://93.184.216.34/news.atom",
        "http://[2606:4700:4700::1111]/index.xml"
    ]
    for url in safe_ip_urls:
        check(is_safe_public_url(url), f"Validation Failure: Safe IP literal was blocked: {url}")
        print(f"  ✓ Allowed safe public IP literal: {url}")

    # Verify public domain URLs pass validation with mocked DNS for offline/sandbox resilience
    def mock_dns(host, port, *args, **kwargs):
        if host in ["jnovels.com", "kitsu.io", "graphql.anilist.co", "example.com"]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 80))]
        return socket.getaddrinfo(host, port, *args, **kwargs)

    safe_domain_urls = [
        "https://jnovels.com/category/light-novel-pdf/",
        "https://kitsu.io/api/edge/manga",
        "https://graphql.anilist.co",
        "http://example.com/feed.xml"
    ]
    with patch("socket.getaddrinfo", side_effect=mock_dns):
        for url in safe_domain_urls:
            check(is_safe_public_url(url), f"Validation Failure: Safe domain URL was blocked: {url}")
            print(f"  ✓ Allowed safe public domain URL (mock DNS): {url}")

    # Adversarial DNS resolution verification
    print("  --- Adversarial DNS Resolution Tests ---")
    # 1. DNS rebinding to loopback
    with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]):
        check(not is_safe_public_url("https://malicious-rebinding.com/"), "DNS rebinding to loopback was not blocked")
        print("  ✓ Blocked DNS rebinding to loopback 127.0.0.1")

    # 2. DNS resolution to RFC 1918 private subnet
    with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 80))]):
        check(not is_safe_public_url("https://internal-alias.com/"), "Resolution to RFC 1918 was not blocked")
        print("  ✓ Blocked resolution to RFC 1918 private IP 192.168.1.1")

    # 3. DNS resolution to cloud metadata
    with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80))]):
        check(not is_safe_public_url("https://metadata-spoof.com/"), "Resolution to Cloud metadata was not blocked")
        print("  ✓ Blocked resolution to Cloud metadata IP 169.254.169.254")

    # 4. Multi-homed response with one safe and one private IP
    with patch("socket.getaddrinfo", return_value=[
        (socket.AF_INET, 0, 0, "", ("93.184.216.34", 0)),
        (socket.AF_INET, 0, 0, "", ("10.0.0.1", 0))
    ]):
        check(not is_safe_public_url("https://dual.example.com/"), "Multi-homed domain with private IP was not blocked")
        print("  ✓ Blocked multi-homed domain containing private IP 10.0.0.1")

    # 5. DNS resolution failure (fail-closed)
    with patch("socket.getaddrinfo", side_effect=socket.gaierror(11001, "getaddrinfo failed")):
        check(not is_safe_public_url("https://nxdomain.invalid/"), "Unresolvable domain did not fail closed")
        print("  ✓ Gracefully failed-closed on socket.gaierror")


def test_safe_http_get_ssrf_blocking():
    print("\n--- Testing safe_http_get Exception Handling ---")
    try:
        safe_http_get("http://127.0.0.1:8080/admin")
        check(False, "Should have raised ValueError on SSRF target")
    except ValueError as e:
        check("SSRF blocked" in str(e), f"Expected 'SSRF blocked' in exception message, got: {e}")
        print("  ✓ safe_http_get raised ValueError on loopback SSRF target")

    try:
        safe_http_get("file:///etc/passwd")
        check(False, "Should have raised ValueError on file:// URI")
    except ValueError as e:
        check("SSRF blocked" in str(e), f"Expected 'SSRF blocked' in exception message, got: {e}")
        print("  ✓ safe_http_get raised ValueError on file:// URI target")

    try:
        safe_http_get("http://169.254.169.254/latest/meta-data/")
        check(False, "Should have raised ValueError on cloud metadata SSRF target")
    except ValueError as e:
        check("SSRF blocked" in str(e), f"Expected 'SSRF blocked' in exception message, got: {e}")
        print("  ✓ safe_http_get raised ValueError on cloud metadata target")


def test_lfi_prevention_rss_provider():
    print("\n--- Testing LFI Prevention in RSSFeedProvider ---")
    lfi_targets = [
        "file:///etc/passwd",
        "file:///C:/Windows/System32/drivers/etc/hosts",
        "file:///C:/Users/rasmu/Documents/AI-Workspace/Web-Scraper/backend/requirements.txt"
    ]
    for target in lfi_targets:
        provider = RSSFeedProvider(name="LFI Test", url=target)
        releases = provider.fetch_releases()
        check(releases == [], f"LFI attack succeeded or was not blocked: {target}")
        print(f"  ✓ Blocked LFI attempt via RSSFeedProvider: {target}")


def test_webhook_security_and_sanitization():
    print("\n--- Testing Webhook SSRF & Error Body Sanitization ---")
    ns = NotificationService()

    # 1. SSRF target via test_webhook
    res = ns.test_webhook(discord_url="http://169.254.169.254/latest/meta-data/")
    check(res["status"] == "error", f"Expected error status, got: {res.get('status')}")
    check("discord" in res["results"], "discord key missing in results")
    check(res["results"]["discord"]["success"] is False, "discord webhook did not fail")
    check("SSRF" in res["results"]["discord"]["message"] or "Blocked" in res["results"]["discord"]["message"],
          f"Unexpected message: {res['results']['discord']['message']}")
    print("  ✓ Blocked Discord webhook pointing to cloud metadata (SSRF)")

    res_gen = ns.test_webhook(apprise_url="http://10.0.0.1:8000/api")
    check(res_gen["status"] == "error", f"Expected error status, got: {res_gen.get('status')}")
    check("generic" in res_gen["results"], "generic key missing in results")
    check(res_gen["results"]["generic"]["success"] is False, "generic webhook did not fail")
    check("SSRF" in res_gen["results"]["generic"]["message"] or "Blocked" in res_gen["results"]["generic"]["message"],
          f"Unexpected message: {res_gen['results']['generic']['message']}")
    print("  ✓ Blocked generic webhook pointing to private RFC 1918 subnet (SSRF)")

    # 2. Verify no raw response body leakage
    check("latest/meta-data" not in res["message"], "Metadata leaked in webhook message")
    print("  ✓ Verified webhook diagnostics do not leak response bodies")


def test_calibre_path_sanitization():
    print("\n--- Testing Calibre Path Traversal & File Sanitization ---")
    # Null bytes
    try:
        sanitize_calibre_path("C:/calibre/\x00evil.db")
        check(False, "Should have rejected path with null byte")
    except ValueError as e:
        check("null byte" in str(e).lower(), f"Expected 'null byte' in error, got: {e}")
        print("  ✓ Rejected null byte in Calibre path")

    # Empty path
    try:
        sanitize_calibre_path("")
        check(False, "Should have rejected empty path")
    except ValueError:
        print("  ✓ Rejected empty Calibre path")

    # Sensitive/executable extension probing
    forbidden_probes = [
        "C:/app/.env",
        "C:/Windows/system32/cmd.exe",
        "C:/keys/server.key",
        "C:/certs/root.crt",
        "C:/config/app.ini"
    ]
    for probe in forbidden_probes:
        try:
            sanitize_calibre_path(probe)
            check(False, f"Should have rejected probing of sensitive file: {probe}")
        except ValueError as e:
            check("disallowed" in str(e).lower(), f"Expected 'disallowed' in error, got: {e}")
            print(f"  ✓ Blocked probing of sensitive file: {probe}")

    # Valid paths
    valid_paths = [
        "C:\\Users\\rasmu\\Calibre Library",
        "C:\\calibre\\metadata.db",
        "/var/lib/calibre"
    ]
    for vp in valid_paths:
        cleaned = sanitize_calibre_path(vp)
        check(cleaned is not None, f"Cleaned path should not be None for {vp}")
        print(f"  ✓ Successfully sanitized valid Calibre path: {vp}")


def test_dom_xss_sanitization():
    print("\n--- Testing Frontend DOM XSS Sanitization ---")
    discovery_html_path = os.path.join(BASE_DIR, "backend", "app", "templates", "discovery.html")
    feeds_html_path = os.path.join(BASE_DIR, "backend", "app", "templates", "feeds.html")

    with open(discovery_html_path, "r", encoding="utf-8") as f:
        discovery_src = f.read()

    # In discovery.html, synopsis should use safe element creation or textContent, not raw .outerHTML injection
    check("box.outerHTML" not in discovery_src or "synDiv" in discovery_src,
          "discovery.html still has unescaped outerHTML injection")
    check("synDiv.textContent" in discovery_src,
          "discovery.html missing safe textContent assignment for synopsis")
    print("  ✓ discovery.html uses safe DOM element creation and textContent for dynamic synopses")

    with open(feeds_html_path, "r", encoding="utf-8") as f:
        feeds_src = f.read()

    check("function escapeHtml" in feeds_src, "feeds.html missing escapeHtml utility function")
    check("function sanitizeLink" in feeds_src, "feeds.html missing sanitizeLink utility function")
    check("escapeHtml(item.title)" in feeds_src, "feeds.html missing HTML escaping on item.title")
    check("safeLink" in feeds_src, "feeds.html missing safe link sanitization on feed links")
    print("  ✓ feeds.html uses escapeHtml and sanitizeLink on dynamic feed preview rendering")


def test_dependency_minimization():
    print("\n--- Testing Dependency Minimization in requirements.txt ---")
    req_path = os.path.join(BASE_DIR, "backend", "requirements.txt")
    with open(req_path, "r", encoding="utf-8") as f:
        req_lines = [l.strip() for l in f.readlines() if l.strip() and not l.startswith("#")]

    # Exactly 7 dependencies
    check(len(req_lines) == 7, f"Expected exactly 7 dependencies, found {len(req_lines)}: {req_lines}")

    # Verify python-multipart and redundant pydantic are completely gone
    for line in req_lines:
        check(not line.startswith("python-multipart"), "python-multipart must be removed")
        check(not line.startswith("pydantic"), "redundant top-level pydantic must be removed")

    expected_pkgs = {"fastapi", "uvicorn[standard]", "jinja2", "httpx", "beautifulsoup4", "feedparser", "apscheduler"}
    manifest_pkgs = {line.split(">=")[0] for line in req_lines}
    check(manifest_pkgs == expected_pkgs, f"Manifest mismatch: {manifest_pkgs} != {expected_pkgs}")
    print(f"  ✓ Verified requirements.txt contains exactly 7 optimized packages: {sorted(list(expected_pkgs))}")


def test_jnovels_master_index_indentation_fix():
    print("\n--- Testing J-Novels Master Index Indentation Fix (Production Method) ---")
    mock_html = """
    <html>
    <body>
        <div class="entry-content">
            <p>Welcome to the master index!</p>
            <p><a href="https://jnovels.com/danmachi-light-novel-epub/">DanMachi Light Novel Epub</a></p>
            <p><a href="https://jnovels.com/overlord-light-novel-epub/">Overlord Volume 1-16 Epub</a></p>
            <p><a href="https://jnovels.com/classroom-of-the-elite-epub/">Classroom of the Elite Epub</a></p>
            <p><a href="https://jnovels.com/faq/">FAQ Page (Must be ignored)</a></p>
            <p><a href="https://external.com/other-link">External Link (Must be ignored)</a></p>
        </div>
    </body>
    </html>
    """

    # 1. Test production execution via urllib fallback (standard in calibre-debug environment)
    mock_urllib_resp = MagicMock()
    mock_urllib_resp.read.return_value = mock_html.encode("utf-8")
    mock_urllib_resp.__enter__.return_value = mock_urllib_resp
    mock_urllib_resp.__exit__.return_value = None

    with patch.object(jnovels_mod, "httpx", None):
        with patch("urllib.request.urlopen", return_value=mock_urllib_resp) as mock_urlopen:
            results = JNovelsProvider.fetch_master_index("https://jnovels.com/light-novel-epub-jp/")

    check(mock_urlopen.called, "urllib.request.urlopen was not called")
    check(len(results) == 3, f"Expected 3 series extracted from master index, got {len(results)}")
    check(all(isinstance(r, ReleaseItem) for r in results), "Results must be ReleaseItem instances")

    extracted_names = [r.series_name for r in results]
    check("DanMachi" in extracted_names, "DanMachi missing from extracted names")
    check("Overlord" in extracted_names, "Overlord missing from extracted names")
    check("Classroom of the Elite" in extracted_names, "Classroom of the Elite missing from extracted names")
    check(all(r.raw_data.get("is_master_index") is True for r in results), "raw_data['is_master_index'] flag missing")
    check(not any("faq" in r.link for r in results), "Blacklisted /faq link was not filtered")
    check(not any("external.com" in r.link for r in results), "External non-JNovels link was not filtered")
    print(f"  ✓ urllib branch: JNovelsProvider.fetch_master_index() successfully parsed {len(results)} series: {extracted_names}")

    # 2. Test production execution via httpx client (standard in production Docker/uvicorn environment)
    mock_httpx = MagicMock()
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = mock_html
    mock_client.get.return_value = mock_resp
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = None
    mock_httpx.Client.return_value = mock_client

    with patch.object(jnovels_mod, "httpx", mock_httpx):
        httpx_results = JNovelsProvider.fetch_master_index("https://jnovels.com/light-novel-epub-jp/")

    check(len(httpx_results) == 3, f"Expected 3 series extracted via httpx, got {len(httpx_results)}")
    check([r.series_name for r in httpx_results] == extracted_names, "Parsed series names mismatch between transports")
    print(f"  ✓ httpx branch: JNovelsProvider.fetch_master_index() successfully parsed {len(httpx_results)} series: {extracted_names}")

    # 3. Test production network error resilience (returns empty list on failure without crashing)
    with patch.object(jnovels_mod, "httpx", None):
        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            error_results = JNovelsProvider.fetch_master_index("https://jnovels.com/light-novel-epub-jp/")
            check(error_results == [], "Expected empty list on network error")
    print("  ✓ Network error resilience: fetch_master_index() gracefully returned empty list on failure")


class _StubRedirectResponse:
    """Minimal httpx-like response used by the redirect-SSRF regression tests."""

    def __init__(self, status_code: int = 200, text: str = "", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.is_redirect = 300 <= status_code < 400


class _StubRedirectClient:
    """Minimal httpx-like client: maps requested URL -> canned response, records URLs."""

    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get(self, url, **kwargs):
        self.requested.append(url)
        return self.pages.get(url, _StubRedirectResponse(status_code=404))


class _StubHttpxModule:
    """Stand-in for the httpx module exposing a Client() factory returning our stub."""

    def __init__(self, pages):
        self.pages = pages
        self.clients = []

    def Client(self, *args, **kwargs):
        client = _StubRedirectClient(self.pages)
        self.clients.append(client)
        return client


def test_safe_client_get_follows_validated_public_redirects():
    """Public -> public redirect hops are followed and the final response is returned."""
    print("\n--- Testing validated public redirect following (safe_client_get) ---")
    pages = {
        "http://93.184.216.34/start": _StubRedirectResponse(302, headers={"location": "/next"}),
        "http://93.184.216.34/next": _StubRedirectResponse(
            302, headers={"Location": "http://8.8.8.8/final"}
        ),
        "http://8.8.8.8/final": _StubRedirectResponse(200, text="<html>public chain</html>"),
    }
    client = _StubRedirectClient(pages)
    resp = safe_client_get(client, "http://93.184.216.34/start")
    check(resp.status_code == 200, f"Expected final HTTP 200, got {resp.status_code}")
    check("public chain" in resp.text, "Final response body was not returned after public redirects")
    check(
        client.requested
        == ["http://93.184.216.34/start", "http://93.184.216.34/next", "http://8.8.8.8/final"],
        f"Unexpected redirect request sequence: {client.requested}",
    )
    print(f"  ✓ public->public redirect chain parsed via {len(client.requested)} validated hops")


def test_safe_client_get_blocks_unsafe_redirect_hops():
    """A public URL redirecting to metadata/loopback must be refused before requesting the hop."""
    print("\n--- Testing SSRF-via-redirect blocking (safe_client_get) ---")
    cases = [
        ("http://169.254.169.254/latest/meta-data/", "cloud metadata"),
        ("http://127.0.0.1:8080/admin", "loopback"),
        ("http://192.168.1.1/hook", "RFC1918 private"),
    ]
    for target, label in cases:
        pages = {
            "http://93.184.216.34/start": _StubRedirectResponse(302, headers={"location": target}),
        }
        client = _StubRedirectClient(pages)
        caught = False
        try:
            safe_client_get(client, "http://93.184.216.34/start")
        except ValueError as e:
            caught = True
            check(
                "ssrf" in str(e).lower() or "unsafe" in str(e).lower(),
                f"Expected an SSRF/unsafe destination error for {label}, got: {e}",
            )
        check(caught, f"Redirect to {label} destination ({target}) was NOT refused")
        check(target not in client.requested, f"Unsafe {label} hop ({target}) was requested!")
        print(f"  ✓ refused redirect hop to {label} without requesting it")

    # Hop cap: more than 5 consecutive public redirects must abort cleanly.
    pages = {}
    for idx in range(8):
        pages[f"http://93.184.216.34/hop{idx}"] = _StubRedirectResponse(
            302, headers={"location": f"http://93.184.216.34/hop{idx + 1}"}
        )
    pages["http://93.184.216.34/hop8"] = _StubRedirectResponse(200, text="too far")
    client = _StubRedirectClient(pages)
    caught_cap = False
    try:
        safe_client_get(client, "http://93.184.216.34/hop0", max_redirects=5)
    except ValueError as e:
        caught_cap = True
        check("redirect" in str(e).lower(), f"Hop-cap error should mention redirects, got: {e}")
    check(caught_cap, "Redirect hop cap (>5) was not enforced")
    check(len(client.requested) <= 6, f"Hop cap requested too many URLs: {len(client.requested)}")
    print("  ✓ redirect hop cap enforced (max_redirects=5) without requesting destination")


def test_jnovels_provider_redirect_ssrf_hardening():
    """J-Novels master index validates redirect hops before fetching them."""
    print("\n--- Testing J-Novels master-index redirect SSRF hardening ---")
    index_html = """
    <html><body><div class="entry-content">
        <p><a href="https://jnovels.com/danmachi-light-novel-epub/">DanMachi Light Novel Epub</a></p>
        <p><a href="https://jnovels.com/overlord-light-novel-epub/">Overlord Volume 1 Epub</a></p>
    </div></body></html>
    """
    # 1. Public -> public redirect chain still parses the master index.
    pages = {
        "http://93.184.216.34/index-start": _StubRedirectResponse(
            302, headers={"location": "http://8.8.8.8/index-final"}
        ),
        "http://8.8.8.8/index-final": _StubRedirectResponse(200, text=index_html),
    }
    stub = _StubHttpxModule(pages)
    with patch.object(jnovels_mod, "httpx", stub):
        safe_results = JNovelsProvider.fetch_master_index("http://93.184.216.34/index-start")
    safe_names = [r.series_name for r in safe_results]
    check(len(safe_results) == 2, f"Public redirect chain dropped results: {safe_names}")
    check("DanMachi" in safe_names and "Overlord" in safe_names, f"Unexpected parsed series: {safe_names}")
    check(
        "http://8.8.8.8/index-final" in stub.clients[-1].requested,
        "Public redirect target was not followed by the provider",
    )
    print(f"  ✓ public redirect chain still parsed {safe_names}")

    # 2. Public -> cloud metadata / loopback redirect is refused, never requested.
    for unsafe_target, label in (
        ("http://169.254.169.254/latest/meta-data/", "cloud metadata"),
        ("http://127.0.0.1:8080/admin", "loopback"),
    ):
        pages = {
            "http://93.184.216.34/index-start": _StubRedirectResponse(
                302, headers={"location": unsafe_target}
            ),
        }
        stub = _StubHttpxModule(pages)
        with patch.object(jnovels_mod, "httpx", stub):
            blocked_results = JNovelsProvider.fetch_master_index("http://93.184.216.34/index-start")
        check(blocked_results == [], f"Provider returned results for unsafe {label} redirect: {blocked_results}")
        check(
            unsafe_target not in stub.clients[-1].requested,
            f"Provider requested unsafe {label} redirect target {unsafe_target}",
        )
        print(f"  ✓ J-Novels provider refused {label} redirect hop")


def run_all_security_tests():
    print("============================================================")
    print("RUNNING NOVELTRAX SECURITY AUDIT & HARDENING TEST SUITE (M1)")
    print("============================================================")
    test_ssrf_disallowed_urls()
    test_safe_http_get_ssrf_blocking()
    test_lfi_prevention_rss_provider()
    test_webhook_security_and_sanitization()
    test_calibre_path_sanitization()
    test_dom_xss_sanitization()
    test_dependency_minimization()
    test_jnovels_master_index_indentation_fix()
    test_safe_client_get_follows_validated_public_redirects()
    test_safe_client_get_blocks_unsafe_redirect_hops()
    test_jnovels_provider_redirect_ssrf_hardening()
    print("\n============================================================")
    print("ALL MILESTONE 1 SECURITY AUDIT & HARDENING TESTS PASSED (100%)!")
    print("============================================================")


if __name__ == "__main__":
    run_all_security_tests()
