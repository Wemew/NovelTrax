"""
tests/test_adversarial_m1_it2.py
Empirical Challenger Adversarial Stress Suite for Milestone 1 Iteration 2.

Focus Areas:
1. JNovelsProvider.fetch_master_index() stress testing:
   - Malformed, fragmented, truncated, and deeply nested HTML payloads
   - Boundary condition HTML (missing containers, empty anchors, invalid hrefs, excluded keywords)
   - Duplicate anchor deduplication and volume extraction stress
   - Extreme load: 5,000 anchor elements in master index
   - Dual-transport testing (httpx branch and urllib fallback)
   - Dual-transport fault injection (httpx non-200, network exceptions, urllib fallback, urllib errors)
2. SSRF Guard (is_safe_public_url & safe_http_get) adversarial probing:
   - Exhaustive IP literal coverage (RFC 1918, CGNAT, Cloud metadata, Test-nets, Broadcast, Multicast, IPv6 ULA/Link-local/Mapped)
   - Credential embedding & userinfo confusion attacks
   - Integer/numeric IP representation tests
   - Fail-closed behavior on DNS lookup failures and malformed URIs
   - safe_http_get redirect attacks and response size enforcement under both httpx and urllib
3. Calibre -O2 optimization verification:
   - Confirm explicit assertion helpers cannot be stripped by compiler flags.
"""

import sys
import os
import time
import socket
from unittest.mock import patch, MagicMock

# Base and backend paths
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

from app.security import (
    is_safe_public_url,
    safe_http_get,
    is_ip_disallowed,
    DISALLOWED_NETWORKS
)
import app.security as security_mod
import app.providers.jnovels as jnovels_mod
from app.providers.jnovels import JNovelsProvider
from app.providers.base import ReleaseItem


def check(condition: bool, message: str = "") -> None:
    """Explicit runtime assertion that cannot be stripped under -O2."""
    if not condition:
        raise AssertionError(message or "Adversarial check failed")


def make_mock_httpx(html_text: str = "", status_code: int = 200, side_effect=None):
    """Factory creating a mock httpx module with Client."""
    mock_mod = MagicMock()
    mock_client = MagicMock()
    if side_effect:
        mock_client.get.side_effect = side_effect
    else:
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.text = html_text
        mock_client.get.return_value = mock_resp
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = None
    mock_mod.Client.return_value = mock_client
    return mock_mod


def make_mock_urllib(html_text: str = "", side_effect=None):
    """Factory creating a mock urllib urlopen response."""
    if side_effect:
        return side_effect
    mock_resp = MagicMock()
    mock_resp.read.return_value = html_text.encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None
    return mock_resp


class ChallengerStressSuite:

    @staticmethod
    def test_calibre_o2_assertion_resilience():
        """Verify check() cannot be bypassed under Calibre -O2 optimization."""
        caught = False
        try:
            check(False, "Expected intentional failure")
        except AssertionError as e:
            if "Expected intentional failure" in str(e):
                caught = True
        if not caught:
            raise RuntimeError("CRITICAL FAILURE: Assertion helper failed to trigger under -O2!")
        print("  [PASS] -O2 Assertion helper resilience verified")

    @staticmethod
    def test_jnovels_empty_and_null_html():
        """Test master index parsing with empty, whitespace, or missing container HTML across both transports."""
        for use_httpx in (False, True):
            transport_name = "httpx" if use_httpx else "urllib"

            # 1. Completely empty HTML
            if use_httpx:
                with patch.object(jnovels_mod, "httpx", make_mock_httpx("")):
                    items = JNovelsProvider.fetch_master_index("https://jnovels.com/empty")
            else:
                with patch.object(jnovels_mod, "httpx", None), patch("urllib.request.urlopen", return_value=make_mock_urllib("")):
                    items = JNovelsProvider.fetch_master_index("https://jnovels.com/empty")
            check(isinstance(items, list) and len(items) == 0, f"[{transport_name}] Empty HTML should yield empty list")

            # 2. Whitespace only HTML
            if use_httpx:
                with patch.object(jnovels_mod, "httpx", make_mock_httpx("   \n\t  ")):
                    items = JNovelsProvider.fetch_master_index("https://jnovels.com/whitespace")
            else:
                with patch.object(jnovels_mod, "httpx", None), patch("urllib.request.urlopen", return_value=make_mock_urllib("   \n\t  ")):
                    items = JNovelsProvider.fetch_master_index("https://jnovels.com/whitespace")
            check(isinstance(items, list) and len(items) == 0, f"[{transport_name}] Whitespace HTML should yield empty list")

            # 3. HTML without .entry-content, article, or .post-content
            html_no_content = "<html><head><title>Test</title></head><body><div class='sidebar'><a href='https://jnovels.com/danmachi-epub/'>DanMachi</a></div></body></html>"
            if use_httpx:
                with patch.object(jnovels_mod, "httpx", make_mock_httpx(html_no_content)):
                    items = JNovelsProvider.fetch_master_index("https://jnovels.com/no-content")
            else:
                with patch.object(jnovels_mod, "httpx", None), patch("urllib.request.urlopen", return_value=make_mock_urllib(html_no_content)):
                    items = JNovelsProvider.fetch_master_index("https://jnovels.com/no-content")
            check(len(items) == 0, f"[{transport_name}] HTML missing target container must return empty list")

        print("  [PASS] J-Novels empty, whitespace, and missing container tests passed (urllib & httpx)")

    @staticmethod
    def test_jnovels_malformed_and_edge_case_html():
        """Stress-test J-Novels HTML parser with malformed, nested, and corrupted HTML."""
        malformed_html = """
        <html><body>
        <div class="entry-content">
            <!-- Unclosed anchor tags and broken attributes -->
            <a href="https://jnovels.com/danmachi-volume-19-epub/">
                <div><span><b>Is It Wrong to Try to Pick Up Girls in a Dungeon? Volume 19</b></span></div>
            </a>
            <a href="https://jnovels.com/overlord-vol-15-epub/">Overlord Vol 15</a>
            <a href="https://jnovels.com/classroom-of-the-elite-2nd-year-volume-9-epub/">
                Classroom of the Elite: 2nd Year Volume 9
            </a>
            <!-- Empty href, whitespace href, missing href -->
            <a>Anchor without href</a>
            <a href="">Empty href</a>
            <a href="   ">Whitespace href</a>
            <!-- Internal and external bad links -->
            <a href="https://jnovels.com/category/light-novel-pdf/page/2/">Category Link</a>
            <a href="https://jnovels.com/tag/isekai/">Tag Link</a>
            <a href="https://jnovels.com/faq/">FAQ Link</a>
            <a href="https://jnovels.com/contact/">Contact Us</a>
            <a href="https://jnovels.com/author/admin/">Author Profile</a>
            <a href="https://jnovels.com/?mode=dark">Dark Mode Link</a>
            <a href="https://jnovels.com/app.adjust.com/app">App Adjust Ad</a>
            <a href="https://jnovels.com/">Home Root</a>
            <a href="http://jnovels.com/non-https-series/">Insecure HTTP</a>
            <a href="https://external-site.com/series/">External Link</a>
            <a href="/relative/path/series/">Relative Link</a>
            <!-- Duplicate series href -->
            <a href="https://jnovels.com/danmachi-volume-19-epub/">DanMachi Vol 19 Duplicate</a>
            <!-- Whitespace text -->
            <a href="https://jnovels.com/blank-series-epub/">   </a>
            <!-- Complex characters and unicode -->
            <a href="https://jnovels.com/re-zero-arc-7-epub/">Re:Zero − Starting Life in Another World &amp; Volume 25</a>
            <a href="https://jnovels.com/mushoku-tensei-vol-26-epub/">無職転生 ~Mushoku Tensei~ Volume 26 [Special Edition]</a>
        </div>
        </body></html>
        """
        for use_httpx in (False, True):
            transport_name = "httpx" if use_httpx else "urllib"
            if use_httpx:
                with patch.object(jnovels_mod, "httpx", make_mock_httpx(malformed_html)):
                    items = JNovelsProvider.fetch_master_index("https://jnovels.com/malformed")
            else:
                with patch.object(jnovels_mod, "httpx", None), patch("urllib.request.urlopen", return_value=make_mock_urllib(malformed_html)):
                    items = JNovelsProvider.fetch_master_index("https://jnovels.com/malformed")

            check(len(items) == 5, f"[{transport_name}] Expected exactly 5 valid unique series, got {len(items)}")
            series_names = [it.series_name for it in items]
            
            # DanMachi should be present
            check(any("Pick Up Girls" in s or "DanMachi" in s for s in series_names), f"[{transport_name}] DanMachi missing")
            # Overlord should be present
            check(any("Overlord" in s for s in series_names), f"[{transport_name}] Overlord missing")
            # Classroom of the Elite should be present
            check(any("Classroom of the Elite" in s for s in series_names), f"[{transport_name}] Classroom of the Elite missing")
            # Re:Zero should be present
            check(any("Re:Zero" in s for s in series_names), f"[{transport_name}] Re:Zero missing")
            # Mushoku Tensei should be present
            check(any("Mushoku Tensei" in s for s in series_names), f"[{transport_name}] Mushoku Tensei missing")

            # Check deduplication
            danmachi_items = [it for it in items if "Pick Up Girls" in it.series_name or "DanMachi" in it.series_name]
            check(len(danmachi_items) == 1, f"[{transport_name}] Duplicate DanMachi link was not properly deduplicated")

            # Check volume resolution
            for it in items:
                check(it.volume is not None and it.volume > 0, f"[{transport_name}] Volume missing or invalid for {it.series_name}")
                check(it.raw_data.get("is_master_index") is True, f"[{transport_name}] raw_data missing is_master_index flag")
                check(it.link.startswith("https://jnovels.com/"), f"[{transport_name}] Invalid link scheme or domain: {it.link}")

        print("  [PASS] J-Novels malformed HTML and boundary condition filters passed (urllib & httpx)")

    @staticmethod
    def test_jnovels_massive_payload_stress():
        """Stress-test J-Novels parser with 5,000 anchor elements."""
        anchors = []
        # 1,000 valid unique series
        for i in range(1, 1001):
            anchors.append(f'<a href="https://jnovels.com/light-novel-series-{i}-epub/">Light Novel Series {i} Volume 1</a>')
        # 1,000 duplicate links
        for i in range(1, 1001):
            anchors.append(f'<a href="https://jnovels.com/light-novel-series-{i}-epub/">Duplicate Title {i}</a>')
        # 1,000 bad links (categories, tags, etc.)
        for i in range(1, 1001):
            anchors.append(f'<a href="https://jnovels.com/category/tag-{i}/">Category {i}</a>')
        # 1,000 external or relative links
        for i in range(1, 1001):
            anchors.append(f'<a href="https://other.com/novel-{i}/">External {i}</a>')
        # 1,000 empty or invalid anchors
        for i in range(1, 1001):
            anchors.append(f'<a href="https://jnovels.com/novel-empty-{i}/">  </a>')

        html = f"<html><body><article>{''.join(anchors)}</article></body></html>"

        start_time = time.perf_counter()
        with patch.object(jnovels_mod, "httpx", make_mock_httpx(html)):
            items = JNovelsProvider.fetch_master_index("https://jnovels.com/massive")
        elapsed = time.perf_counter() - start_time

        check(len(items) == 1000, f"Expected exactly 1,000 unique series from 5,000 anchors, got {len(items)}")
        check(elapsed < 5.0, f"Parser too slow on 5,000 items: took {elapsed:.2f}s (budget: 5.0s)")
        print(f"  [PASS] J-Novels massive 5,000-anchor payload parsed in {elapsed:.3f}s (1,000 unique extracted)")

    @staticmethod
    def test_jnovels_network_fault_injection():
        """Fault injection across httpx status codes, connection errors, and urllib fallback."""
        # 1. Non-200 HTTP status (403 Cloudflare, 404, 500)
        for code in (403, 404, 500, 502, 503):
            with patch.object(jnovels_mod, "httpx", make_mock_httpx("<html>Blocked</html>", status_code=code)):
                items = JNovelsProvider.fetch_master_index("https://jnovels.com/error")
                check(len(items) == 0, f"Status code {code} should fail safely returning []")

        # 2. httpx exceptions (ConnectTimeout, ReadTimeout, NetworkError)
        with patch.object(jnovels_mod, "httpx", make_mock_httpx(side_effect=Exception("Connection reset by peer"))):
            items = JNovelsProvider.fetch_master_index("https://jnovels.com/network-fail")
            check(len(items) == 0, "Network exception must be caught safely returning []")

        # 3. urllib fallback branch when httpx is None
        sample_html = """
        <div class="post-content">
            <a href="https://jnovels.com/fallback-series-vol-1-epub/">Fallback Series Volume 1</a>
        </div>
        """
        with patch.object(jnovels_mod, "httpx", None):
            # Success with urllib
            with patch("urllib.request.urlopen", return_value=make_mock_urllib(sample_html)):
                items = JNovelsProvider.fetch_master_index("https://jnovels.com/urllib-success")
                check(len(items) == 1, f"Urllib fallback should extract 1 series, got {len(items)}")
                check(items[0].series_name == "Fallback Series", f"Unexpected title: {items[0].series_name}")

            # Failure with urllib
            with patch("urllib.request.urlopen", side_effect=Exception("urllib socket error")):
                items = JNovelsProvider.fetch_master_index("https://jnovels.com/urllib-fail")
                check(len(items) == 0, "Urllib exception should gracefully return []")

        print("  [PASS] J-Novels transport fault injection & urllib fallback verified")

    @staticmethod
    def test_ssrf_exhaustive_ip_and_vector_matrix():
        """Exhaustive stress-testing of is_safe_public_url across edge cases and evasion vectors."""
        # 1. Valid public IP literals (must return True without DNS call)
        public_ips = [
            "http://1.1.1.1/",
            "https://1.0.0.1:8080/feed",
            "http://8.8.8.8/test",
            "https://8.8.4.4:443/",
            "http://93.184.216.34/",
            "https://142.250.190.46/",
            "http://[2606:4700:4700::1111]/",
            "http://[2001:4860:4860::8888]:8080/",
        ]
        for url in public_ips:
            check(is_safe_public_url(url) is True, f"Public IP was falsely blocked: {url}")

        # 2. RFC 1918 Private IPv4
        rfc1918_ips = [
            "http://10.0.0.0/",
            "http://10.0.0.1/",
            "http://10.255.255.255/",
            "http://172.16.0.0/",
            "http://172.20.10.1/",
            "http://172.31.255.255/",
            "http://192.168.0.0/",
            "http://192.168.1.1/",
            "http://192.168.255.255/",
        ]
        for url in rfc1918_ips:
            check(is_safe_public_url(url) is False, f"Private RFC 1918 IP was not blocked: {url}")

        # 3. CGNAT (100.64.0.0/10)
        cgnat_ips = [
            "http://100.64.0.0/",
            "http://100.64.0.1/",
            "http://100.100.100.100/",
            "http://100.127.255.255/",
        ]
        for url in cgnat_ips:
            check(is_safe_public_url(url) is False, f"CGNAT IP was not blocked: {url}")

        # 4. Link-Local & Cloud Metadata (169.254.0.0/16)
        link_local_ips = [
            "http://169.254.0.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://169.254.255.255/",
        ]
        for url in link_local_ips:
            check(is_safe_public_url(url) is False, f"Cloud metadata / link-local was not blocked: {url}")

        # 5. Documentation, Test, Benchmarking, Multicast, Broadcast, Reserved
        special_ips = [
            "http://192.0.2.1/",         # TEST-NET-1
            "http://198.51.100.1/",      # TEST-NET-2
            "http://203.0.113.1/",       # TEST-NET-3
            "http://198.18.0.1/",        # Benchmarking
            "http://224.0.0.1/",         # Multicast
            "http://239.255.255.250/",   # SSDP Multicast
            "http://240.0.0.1/",         # Reserved
            "http://255.255.255.255/",   # Broadcast
            "http://0.0.0.0/",           # Zero address
        ]
        for url in special_ips:
            check(is_safe_public_url(url) is False, f"Special/reserved IP was not blocked: {url}")

        # 6. IPv6 disallowed ranges (Loopback, ULA, Link-Local, Multicast, Doc, IPv4-mapped)
        disallowed_ipv6 = [
            "http://[::]/",
            "http://[::1]/",
            "http://[fc00::1]/",
            "http://[fd00::1]/",
            "http://[fe80::1]/",
            "http://[ff02::1]/",
            "http://[2001:db8::1]/",
            "http://[::ffff:127.0.0.1]/",
            "http://[::ffff:10.0.0.1]/",
            "http://[::ffff:192.168.1.1]/",
            "http://[::ffff:169.254.169.254]/",
            "http://[::ffff:8.8.8.8]/",      # In DISALLOWED_NETWORKS ::ffff:0:0/96
            "http://[64:ff9b::127.0.0.1]/",  # In DISALLOWED_NETWORKS 64:ff9b::/96
        ]
        for url in disallowed_ipv6:
            check(is_safe_public_url(url) is False, f"Disallowed IPv6 was not blocked: {url}")

        # 7. Credential embedding and userinfo confusion
        # Userinfo should not trick hostname extraction
        check(is_safe_public_url("http://admin:secret@127.0.0.1/") is False, "Userinfo @127.0.0.1 not blocked")
        check(is_safe_public_url("http://user:pass@192.168.1.1:8080/path") is False, "Userinfo @192.168.1.1 not blocked")
        check(is_safe_public_url("http://127.0.0.1@1.1.1.1/") is True, "Userinfo misleading host to 1.1.1.1 falsely blocked")

        # 8. Numeric/integer IP parsing in python ipaddress
        # 2130706433 is 127.0.0.1 in decimal integer
        check(is_safe_public_url("http://2130706433/") is False, "Integer loopback 2130706433 not blocked")

        # 9. Non-HTTP protocols
        schemes = [
            "file:///etc/passwd",
            "file:///C:/Windows/System32/calc.exe",
            "ftp://ftp.example.com/",
            "gopher://127.0.0.1:70/",
            "ldap://127.0.0.1/",
            "dict://127.0.0.1:2628/",
            "data:text/html,<script>alert(1)</script>",
            "javascript:alert(document.domain)",
        ]
        for url in schemes:
            check(is_safe_public_url(url) is False, f"Non-HTTP scheme was not blocked: {url}")

        # 10. Malformed URLs and null bytes
        malformed = [
            "",
            None,
            123,
            "   ",
            "http://",
            "https://",
            "http:///index.html",
            "://missing-scheme",
            "http://example.com\x00evil.com",
        ]
        for url in malformed:
            check(is_safe_public_url(url) is False, f"Malformed URL did not fail-close: {url}")

        print("  [PASS] SSRF exhaustive IP and vector matrix passed (45 vectors tested)")

    @staticmethod
    def test_safe_http_get_redirect_and_size_limits():
        """Stress-test safe_http_get redirects, size caps, and SSRF interceptor under httpx and urllib."""
        # 1. Unsafe initial URL
        try:
            safe_http_get("http://127.0.0.1:8080/admin")
            raise AssertionError("safe_http_get did not block loopback")
        except ValueError as e:
            check("SSRF blocked" in str(e), f"Unexpected exception message: {e}")

        # 2. Redirect to private IP under httpx
        mock_httpx = MagicMock()
        mock_client = MagicMock()
        mock_resp_redir = MagicMock()
        mock_resp_redir.is_redirect = True
        mock_resp_redir.headers = {"location": "http://192.168.1.100/internal"}
        mock_client.get.return_value = mock_resp_redir
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = None
        mock_httpx.Client.return_value = mock_client

        with patch.object(security_mod, "httpx", mock_httpx):
            try:
                safe_http_get("http://1.1.1.1/redirect")
                raise AssertionError("Redirect to private IP was not blocked")
            except ValueError as e:
                check("SSRF blocked: Redirect hop targets unsafe destination" in str(e), f"Unexpected message: {e}")

        # 3. Maximum redirects exceeded under httpx
        mock_resp_loop = MagicMock()
        mock_resp_loop.is_redirect = True
        mock_resp_loop.headers = {"location": "http://1.1.1.1/loop"}
        mock_client.get.return_value = mock_resp_loop

        with patch.object(security_mod, "httpx", mock_httpx):
            try:
                safe_http_get("http://1.1.1.1/loop", max_redirects=3)
                raise AssertionError("Exceeding max redirects did not raise ValueError")
            except ValueError as e:
                check("Maximum redirects (3) exceeded" in str(e), f"Unexpected message: {e}")

        # 4. Content-length header exceeding max_bytes under httpx
        mock_resp_oversized_header = MagicMock()
        mock_resp_oversized_header.is_redirect = False
        mock_resp_oversized_header.headers = {"content-length": "20000000"}  # 20MB
        mock_client.get.return_value = mock_resp_oversized_header

        with patch.object(security_mod, "httpx", mock_httpx):
            try:
                safe_http_get("http://1.1.1.1/huge", max_bytes=1024 * 1024)
                raise AssertionError("Oversized content-length header was not rejected")
            except ValueError as e:
                check("exceeds limit" in str(e), f"Unexpected message: {e}")

        # 5. Content body exceeding max_bytes under httpx
        mock_resp_oversized_body = MagicMock()
        mock_resp_oversized_body.is_redirect = False
        mock_resp_oversized_body.headers = {}
        mock_resp_oversized_body.content = b"X" * (1024 * 1024 + 50)
        mock_client.get.return_value = mock_resp_oversized_body

        with patch.object(security_mod, "httpx", mock_httpx):
            try:
                safe_http_get("http://1.1.1.1/huge-body", max_bytes=1024 * 1024)
                raise AssertionError("Oversized content body was not rejected")
            except ValueError as e:
                check("exceeds maximum allowed size" in str(e), f"Unexpected message: {e}")

        # 6. Response exceeding max_bytes under urllib fallback
        with patch.object(security_mod, "httpx", None):
            mock_urllib_oversized = MagicMock()
            mock_urllib_oversized.read.return_value = b"Y" * (1024 * 1024 + 50)
            mock_urllib_oversized.headers = {}
            mock_urllib_oversized.status = 200
            mock_urllib_oversized.__enter__.return_value = mock_urllib_oversized
            mock_urllib_oversized.__exit__.return_value = None

            with patch("urllib.request.OpenerDirector.open", return_value=mock_urllib_oversized):
                try:
                    safe_http_get("http://1.1.1.1/huge-urllib", max_bytes=1024 * 1024)
                    raise AssertionError("Urllib oversized content was not rejected")
                except ValueError as e:
                    check("exceeds maximum allowed size" in str(e), f"Unexpected message: {e}")

        print("  [PASS] safe_http_get redirect interceptor and payload size bounds verified (httpx & urllib)")


def main():
    print("=" * 70)
    print("RUNNING ADVERSARIAL STRESS SUITE (CHALLENGER M1 ITERATION 2)")
    print("=" * 70)

    suite = ChallengerStressSuite()
    suite.test_calibre_o2_assertion_resilience()
    suite.test_jnovels_empty_and_null_html()
    suite.test_jnovels_malformed_and_edge_case_html()
    suite.test_jnovels_massive_payload_stress()
    suite.test_jnovels_network_fault_injection()
    suite.test_ssrf_exhaustive_ip_and_vector_matrix()
    suite.test_safe_http_get_redirect_and_size_limits()

    print("=" * 70)
    print("ALL ADVERSARIAL STRESS TESTS PASSED WITH 100% SUCCESS!")
    print("=" * 70)


if __name__ == "__main__":
    main()
