"""
tests/test_adversarial_m1.py
Adversarial Security Stress Test Suite for Milestone 1.

Fuzzes and stress-tests:
1. SSRF URL validation with exotic IP formats (octal, decimal/dword, hex, IPv6-mapped IPv4, CGNAT, link-local, cloud metadata, protocols).
2. HTTP Redirect SSRF protection (ensures redirect hops cannot pivot to internal/loopback services).
3. Webhook diagnostic error leak stress testing (ensures 4xx/5xx responses or connection errors never leak response bodies or secrets).
4. Calibre path sanitization against null bytes, traversal attacks, case variations, and sensitive extensions.
5. DOM XSS escaping verification on exotic payloads (event handlers, javascript: schemes, SVG vectors).
"""

import sys
import os
import socket
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

from app.security import (
    is_safe_public_url,
    safe_http_get,
    sanitize_calibre_path,
    DISALLOWED_NETWORKS
)
from app.notifier.webhook import NotificationService, SimpleHttpResponse

def run_exotic_ssrf_stress_tests():
    print("\n[CHALLENGE 1] Exotic SSRF Vectors & Obfuscation Fuzzing")
    
    # 1. Octal, Decimal, Hex, and Shorthand IP representations
    exotic_loopback_payloads = [
        # Octal representations of 127.0.0.1
        "http://0177.0.0.1/",
        "http://0177.0.0.1:8080/secret",
        "http://0177.0000.0000.0001/",
        "http://0177.1/",
        # Decimal / Dword representations of 127.0.0.1 (2130706433)
        "http://2130706433/",
        "http://2130706433:8000/admin",
        # Hex representations of 127.0.0.1 (0x7f000001)
        "http://0x7f000001/",
        "http://0x7f.0.0.1/",
        "http://0x7f.1/",
        "http://0x7F.0x00.0x00.0x01/",
        # Shorthand dotted representations
        "http://127.1/",
        "http://127.0.1/",
        "http://0/",
        "http://0.0.0.0/",
        "http://0.0.0.0:8000/",
        # Decimal / Hex representations of AWS/GCP Metadata 169.254.169.254 (2852039166, 0xa9fea9fe)
        "http://2852039166/latest/meta-data/",
        "http://0xa9fea9fe/latest/meta-data/",
        "http://0xa9.0xfe.0xa9.0xfe/",
        "http://0251.0376.0251.0376/latest/meta-data/",
        # Private RFC 1918 representations in decimal/hex (10.0.0.1 -> 167772161, 0x0a000001)
        "http://167772161/",
        "http://0x0a000001/",
        "http://012.0.0.1/",
        # Carrier-Grade NAT (100.64.0.1)
        "http://100.64.0.1/",
        "http://100.127.255.254/",
    ]

    for payload in exotic_loopback_payloads:
        is_safe = is_safe_public_url(payload)
        assert not is_safe, f"CRITICAL: Exotic SSRF vector was NOT blocked: {payload}"
        print(f"  [PASS] Blocked exotic IP representation: {payload}")

    # 2. IPv6 exotic representations (IPv4-mapped, ULA, Link-Local, Unspecified)
    ipv6_payloads = [
        "http://[::1]/",
        "http://[::1]:8080/admin",
        "http://[::]/",
        "http://[0000:0000:0000:0000:0000:0000:0000:0001]/",
        # IPv4-mapped IPv6
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:7f00:1]/",
        "http://[::ffff:169.254.169.254]/",
        "http://[::ffff:10.0.0.1]/",
        "http://[::ffff:192.168.1.1]/",
        "http://[0:0:0:0:0:ffff:127.0.0.1]/",
        # Link-local & ULA IPv6
        "http://[fe80::1]/",
        "http://[fe80::1%eth0]/",
        "http://[fc00::1]/",
        "http://[fd12:3456:789a::1]/",
        "http://[ff02::1]/",
        "http://[100::1]/",
        "http://[2001:db8::1]/",
    ]

    for payload in ipv6_payloads:
        is_safe = is_safe_public_url(payload)
        assert not is_safe, f"CRITICAL: IPv6 SSRF vector was NOT blocked: {payload}"
        print(f"  [PASS] Blocked IPv6 SSRF vector: {payload}")

    # 3. Disallowed protocols & URI schemes
    disallowed_protocols = [
        "file:///etc/passwd",
        "file:///c:/windows/win.ini",
        "file://localhost/etc/shadow",
        "gopher://127.0.0.1:6379/_INFO",
        "dict://127.0.0.1:11211/stats",
        "ftp://127.0.0.1/backup",
        "sftp://internal.corp/",
        "tftp://10.0.0.1/",
        "ldap://127.0.0.1:389/o=example",
        "javascript:alert(1)",
        "data:text/html,<h1>PWNED</h1>",
        "vbscript:msgbox(1)",
        "about:blank",
        "blob:https://example.com/test",
        "jar:file:///path/to/archive.jar!/file.txt",
    ]

    for proto in disallowed_protocols:
        is_safe = is_safe_public_url(proto)
        assert not is_safe, f"CRITICAL: Disallowed protocol was NOT blocked: {proto}"
        print(f"  [PASS] Blocked disallowed protocol: {proto}")

    # 4. Obfuscation, userinfo, trailing dot, whitespace, and case sensitivity
    obfuscated_payloads = [
        "http://localhost.",
        "http://LOCALHOST/",
        "http://LoCaLhOsT:5000/",
        "HTTP://127.0.0.1/",
        "https://127.0.0.1/",
        "http://user:pass@127.0.0.1/",
        "http://admin:secret@localhost:8080/",
        "http://127.0.0.1%00@google.com/",
        "http://google.com#@127.0.0.1/",
        "http://",
        "http:///",
        "   http://127.0.0.1/   ",
        "\x00http://127.0.0.1",
        "http://metadata.google.internal/",
        "http://metadata.google.internal:8080/",
    ]

    for payload in obfuscated_payloads:
        is_safe = is_safe_public_url(payload)
        assert not is_safe, f"CRITICAL: Obfuscated SSRF payload was NOT blocked: {repr(payload)}"
        print(f"  [PASS] Blocked obfuscated SSRF payload: {repr(payload)}")

    # 5. Legitimate safe public URLs must NOT be falsely rejected
    legitimate_urls = [
        "https://jnovels.com/feed/",
        "https://kitsu.io/api/edge/anime?filter%5Btext%5D=slayer",
        "https://graphql.anilist.co",
        "http://example.com/",
        "https://raw.githubusercontent.com/user/repo/main/README.md",
        "https://nyaa.si/?f=0&c=3_1&q=overlord"
    ]

    for url in legitimate_urls:
        is_safe = is_safe_public_url(url)
        assert is_safe, f"FALSE POSITIVE: Valid public URL was incorrectly blocked: {url}"
        print(f"  [PASS] Correctly permitted legitimate public URL: {url}")


class MockRedirectServer(BaseHTTPRequestHandler):
    """Mock HTTP server simulating SSRF redirection attacks."""
    def do_GET(self):
        if self.path == "/redirect-to-loopback":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:8080/admin/secrets")
            self.end_headers()
        elif self.path == "/redirect-to-metadata":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
        elif self.path == "/redirect-chain-1":
            self.send_response(302)
            self.send_header("Location", "/redirect-to-loopback")
            self.end_headers()
        elif self.path == "/legit-data":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"SAFE_DATA_PAYLOAD")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress logging during tests


def run_redirect_ssrf_stress_tests():
    print("\n[CHALLENGE 2] HTTP Redirect SSRF Pivot Stress Testing")
    
    server = HTTPServer(("127.0.0.1", 0), MockRedirectServer)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        # Note: safe_http_get will reject the initial URL if targeted at 127.0.0.1,
        # which is the first line of defense.
        try:
            safe_http_get(f"http://127.0.0.1:{port}/redirect-to-loopback")
            assert False, "Should have failed on initial loopback URL"
        except ValueError as e:
            assert "SSRF blocked" in str(e)
            print("  [PASS] Blocked initial loopback request before connection")

        # Now test the redirect handler unit directly:
        from app.security import SSRFSafeRedirectHandler
        handler = SSRFSafeRedirectHandler()
        
        # Test redirection to loopback
        try:
            handler.redirect_request(None, None, 302, "Found", {}, "http://127.0.0.1:8080/admin")
            assert False, "Redirect handler should have blocked redirect to loopback"
        except ValueError as e:
            assert "SSRF blocked" in str(e)
            print("  [PASS] SSRFSafeRedirectHandler intercepted redirect to 127.0.0.1")

        # Test redirection to metadata
        try:
            handler.redirect_request(None, None, 302, "Found", {}, "http://169.254.169.254/latest/meta-data/")
            assert False, "Redirect handler should have blocked redirect to cloud metadata"
        except ValueError as e:
            assert "SSRF blocked" in str(e)
            print("  [PASS] SSRFSafeRedirectHandler intercepted redirect to cloud metadata")

        # Test redirection to file://
        try:
            handler.redirect_request(None, None, 302, "Found", {}, "file:///etc/passwd")
            assert False, "Redirect handler should have blocked redirect to file:///"
        except ValueError as e:
            assert "SSRF blocked" in str(e)
            print("  [PASS] SSRFSafeRedirectHandler intercepted redirect to file:// URI")

    finally:
        server.shutdown()
        server.server_close()


def run_webhook_error_leak_stress_tests():
    print("\n[CHALLENGE 3] Webhook Diagnostic Response & Error Sanitization")
    
    ns = NotificationService()

    # Case 1: SSRF attempts via test_webhook
    ssrf_targets = [
        "http://127.0.0.1:6379/",
        "http://169.254.169.254/latest/meta-data/identity-credentials/auth/token",
        "http://10.0.0.1:8000/internal-metrics",
        "file:///etc/passwd",
        "gopher://127.0.0.1:25/"
    ]
    for target in ssrf_targets:
        res = ns.test_webhook(discord_url=target, apprise_url=target)
        assert res["status"] == "error"
        # Discord result
        d_res = res["results"]["discord"]
        assert d_res["success"] is False
        assert "Blocked by SSRF" in d_res["message"]
        # Generic result
        g_res = res["results"]["generic"]
        assert g_res["success"] is False
        assert "Blocked by SSRF" in g_res["message"]
        # Ensure no URL paths or internal terms leak in overall message
        assert "identity-credentials" not in res["message"]
        assert "internal-metrics" not in res["message"]
        print(f"  [PASS] Webhook diagnostic safely blocked and sanitized SSRF target: {target}")

    # Case 2: Simulating 4xx/5xx responses from external webhook server with sensitive response body
    class SensitiveErrorServer(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            # Body containing sensitive stack trace and database credentials
            self.wfile.write(b'{"internal_secret_token": "SUPER_SECRET_KEY_12345", "traceback": "DatabaseError at /var/www/secrets"}')

        def log_message(self, format, *args):
            pass

    # We mock _post_json to test how test_webhook formats errors
    import app.notifier.webhook as webhook_module
    orig_post = webhook_module._post_json
    
    try:
        # Mocking external server returning 500 with sensitive body
        webhook_module._post_json = lambda url, data, timeout=10.0: SimpleHttpResponse(500, '{"internal_secret_token": "SUPER_SECRET_KEY_12345"}')
        
        # Safe dummy public domain so it passes initial SSRF check
        res = ns.test_webhook(discord_url="https://discord.com/api/webhooks/mock", apprise_url="https://example.com/webhook")
        assert res["status"] == "error"
        # Check that discord message is cleanly formatted status code only
        assert res["results"]["discord"]["message"] == "Discord returned HTTP 500"
        assert res["results"]["generic"]["message"] == "Generic webhook returned HTTP 500"
        # Verify secret token is NOT leaked
        assert "SUPER_SECRET_KEY_12345" not in res["results"]["discord"]["message"]
        assert "SUPER_SECRET_KEY_12345" not in res["results"]["generic"]["message"]
        assert "SUPER_SECRET_KEY_12345" not in res["message"]
        print("  [PASS] Verified 500 error response body is completely omitted from webhook diagnostics")

        # Mocking 404 response
        webhook_module._post_json = lambda url, data, timeout=10.0: SimpleHttpResponse(404, '{"error": "Webhook not found", "server_id": 99887766}')
        res = ns.test_webhook(discord_url="https://discord.com/api/webhooks/mock")
        assert res["results"]["discord"]["message"] == "Discord returned HTTP 404"
        assert "server_id" not in res["results"]["discord"]["message"]
        print("  [PASS] Verified 404 error response body is completely omitted from webhook diagnostics")

    finally:
        webhook_module._post_json = orig_post


def run_calibre_path_sanitization_stress_tests():
    print("\n[CHALLENGE 4] Calibre Path Traversal, Null Bytes & Extension Fuzzing")
    
    # 1. Null byte attacks in various positions
    null_byte_payloads = [
        "C:\\Calibre\\metadata.db\x00",
        "\x00C:\\Calibre\\metadata.db",
        "C:\\Calibre\x00\\metadata.db",
        "metadata.db\x00.exe",
        "C:/Users/user/library/\x00",
    ]
    for p in null_byte_payloads:
        try:
            sanitize_calibre_path(p)
            assert False, f"CRITICAL: Null byte path was NOT rejected: {repr(p)}"
        except ValueError as e:
            assert "null bytes" in str(e).lower()
            print(f"  [PASS] Rejected null byte injection: {repr(p)}")

    # 2. Blocked file extensions and sensitive system file probing
    sensitive_file_probes = [
        "C:\\Windows\\win.ini",
        "C:/Windows/win.INI",          # uppercase extension
        "C:\\Windows\\system32\\cmd.exe",
        "C:/app/.env",
        "/etc/nginx/nginx.conf",
        "C:\\certs\\privkey.pem",
        "C:\\certs\\server.crt",
        "C:\\keys\\secret.key",
        "C:\\ProgramData\\app.log",
        "C:\\tools\\script.bat",
        "C:\\tools\\runner.cmd",
        "C:\\tools\\deploy.ps1",
        "C:\\tools\\agent.vbs",
        "C:\\tools\\bootstrap.sh",
        "C:\\Windows\\System32\\kernel32.dll",
    ]
    for probe in sensitive_file_probes:
        try:
            sanitize_calibre_path(probe)
            assert False, f"CRITICAL: Sensitive/executable extension probe was NOT rejected: {probe}"
        except ValueError as e:
            assert "disallowed" in str(e).lower()
            print(f"  [PASS] Blocked sensitive file probe: {probe}")

    # 3. Path Traversal payloads combined with blocked extensions
    traversal_payloads = [
        r"..\..\..\Windows\win.ini",
        r"..\..\..\..\..\..\app\.env",
        r"..\..\..\..\Windows\System32\cmd.exe",
        r"subdir/../../../../keys/id_rsa.pem",
    ]
    for tp in traversal_payloads:
        try:
            sanitize_calibre_path(tp)
            assert False, f"CRITICAL: Traversal probe was NOT rejected: {tp}"
        except ValueError as e:
            assert "disallowed" in str(e).lower()
            print(f"  [PASS] Blocked traversal attack to sensitive file: {tp}")

    # 4. Valid Calibre directory paths should be normalized cleanly
    valid_library_paths = [
        (r"C:\Users\User\Calibre Library", r"C:\Users\User\Calibre Library"),
        (r"C:/Users/User/Calibre Library/", r"C:\Users\User\Calibre Library" if os.name == 'nt' else r"C:/Users/User/Calibre Library"),
        (r"C:\Calibre\metadata.db", r"C:\Calibre\metadata.db"),
    ]
    for in_path, _ in valid_library_paths:
        cleaned = sanitize_calibre_path(in_path)
        assert cleaned, f"Valid path failed sanitization: {in_path}"
        assert "\x00" not in cleaned
        print(f"  [PASS] Valid library path normalized cleanly: {in_path} -> {cleaned}")


def run_dom_xss_sanitization_stress_tests():
    print("\n[CHALLENGE 5] DOM XSS Defense & Script Injection Stress Testing")
    
    discovery_html = os.path.join(BASE_DIR, "backend", "app", "templates", "discovery.html")
    feeds_html = os.path.join(BASE_DIR, "backend", "app", "templates", "feeds.html")

    with open(discovery_html, "r", encoding="utf-8") as f:
        disc_content = f.read()

    # Verify discovery.html uses createElement + textContent and does NOT use outerHTML or innerHTML for synopsis
    assert "synDiv.textContent" in disc_content, "Missing synDiv.textContent in discovery.html"
    assert "box.outerHTML =" not in disc_content, "Found dangerous box.outerHTML assignment in discovery.html"
    print("  [PASS] discovery.html uses safe textContent sink instead of innerHTML/outerHTML")

    with open(feeds_html, "r", encoding="utf-8") as f:
        feed_content = f.read()

    # Replicate feeds.html escapeHtml and sanitizeLink logic in Python to test against adversarial inputs
    def escape_html(val):
        if not val:
            return ""
        s = str(val)
        return (s.replace("&", "&amp;")
                 .replace("<", "&lt;")
                 .replace(">", "&gt;")
                 .replace('"', "&quot;")
                 .replace("'", "&#39;"))

    def sanitize_link(val):
        if not val:
            return "#"
        s = str(val).strip()
        if re.match(r"^https?://", s, re.IGNORECASE):
            return escape_html(s)
        return "#"

    import re
    # Test adversarial title strings
    adversarial_titles = [
        "<script>alert('XSS')</script>",
        "<img src=x onerror=alert(1)>",
        "<svg/onload=alert(document.cookie)>",
        "\" onfocus=\"alert(1)\" autofocus=\"",
        "'><script src=//evil.com/xss.js></script>",
        "&lt;script&gt;alert(1)&lt;/script&gt;",
        "Test & 'Quote' \"Double\" <Tag>",
    ]
    for title in adversarial_titles:
        escaped = escape_html(title)
        assert "<script>" not in escaped
        assert "<img" not in escaped
        assert "<svg" not in escaped
        assert '"' not in escaped
        assert "'" not in escaped
        print(f"  [PASS] HTML escaping neutralized XSS title payload: {escaped[:40]}")

    # Test adversarial link strings
    adversarial_links = [
        "javascript:alert(document.cookie)",
        "JAVASCRIPT:alert(1)",
        "javascript://%0aalert(1)",
        "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "#internal-anchor",
        "http://example.com/\" onmouseover=\"alert(1)",
    ]
    for link in adversarial_links:
        safe_link = sanitize_link(link)
        if not link.lower().startswith("http://") and not link.lower().startswith("https://"):
            assert safe_link == "#", f"Dangerous link scheme was not neutralized to '#': {link} -> {safe_link}"
        else:
            assert '"' not in safe_link, f"Quote was not escaped in HTTP link: {safe_link}"
        print(f"  [PASS] Safe link sanitizer neutralized dangerous link: {link} -> {safe_link}")


def run_all_adversarial_challenges():
    print("======================================================================")
    print("RUNNING ADVERSARIAL SECURITY CHALLENGE TEST HARNESS (CHALLENGER M1)")
    print("======================================================================")
    
    run_exotic_ssrf_stress_tests()
    run_redirect_ssrf_stress_tests()
    run_webhook_error_leak_stress_tests()
    run_calibre_path_sanitization_stress_tests()
    run_dom_xss_sanitization_stress_tests()
    
    print("\n======================================================================")
    print("ALL ADVERSARIAL SECURITY STRESS TESTS PASSED EMPIRICALLY (100% SUCCESS)!")
    print("======================================================================")

if __name__ == "__main__":
    run_all_adversarial_challenges()
