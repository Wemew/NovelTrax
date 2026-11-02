r"""
tests/test_downloader.py
Automated Test Suite for NovelTrax Milestone 3: Downloader & Queue Management.

Verifies:
- Tier 1: QBitClient Web API v2 Unit Tests (auth, session, add, pause, resume, delete, connection test, hash extraction, SSL, empty hashes)
- Tier 2: Light Novel Filename Parsing & Volume Extraction (single, omnibus ranges, decimal volumes, numbered series guards, ancillary files)
- Tier 3: Selective Volume Downloading Algorithm & Priority Management (subset rule, single archive, pending metadata, all owned, all needed)
- Tier 4: Downloader REST API & UI View Verification (downloads.html, base.html navbar, database migrations and methods, main.py routes)
- Tier 5: Calibre Cross-Reference & End-to-End Selective Download Pipeline

Execution:
  & "C:\Program Files\Calibre2\calibre-debug.exe" -e tests\test_downloader.py
  python tests\test_downloader.py
"""

import os
import sys
import json
import time
import socket
import sqlite3
import tempfile
import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar
from typing import List, Dict, Any, Optional, Set, Tuple

# Ensure backend directory is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.downloader.qbit import (
    QBitClient,
    ConnectionTestResult,
    SelectiveDownloadResult,
    parse_torrent_filename_volumes,
)
from app.security import validate_downloader_destination
from app.database import Database

# Non-strippable assertion helper for Calibre -O2 execution
passed_count = 0
failed_count = 0

def check(condition: bool, msg: str = ""):
    global passed_count, failed_count
    if not condition:
        failed_count += 1
        raise AssertionError(f"Check failed: {msg}")
    passed_count += 1
    print(f"  ✓ {msg}")


class MockHTTPResponse:
    """Hermetic HTTP response fixture for urllib opener mock."""
    def __init__(self, data: bytes = b"", status: int = 200, headers: Optional[Dict[str, str]] = None):
        self._data = data
        self.status = status
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._data

    def decode(self, encoding: str = "utf-8", errors: str = "replace") -> str:
        return self._data.decode(encoding, errors)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class MockUrllibOpener:
    """Hermetic in-memory mock for urllib.request opener."""
    def __init__(self, client: QBitClient):
        self.client = client
        self.requests_log: List[Dict[str, Any]] = []
        self.fail_auth: bool = False
        self.auth_http_403: bool = False
        self.fail_network: bool = False
        self.mock_version: str = "v4.6.5"
        self.mock_torrents: List[Dict[str, Any]] = [
            {"hash": "hash123", "name": "Overlord Vol 16 [EPUB]", "size": 15000000, "progress": 0.75, "state": "downloading", "dlspeed": 2500000, "eta": 120}
        ]
        self.mock_files: List[Dict[str, Any]] = []

    def open(self, req: urllib.request.Request, timeout: float = 10.0) -> MockHTTPResponse:
        url = req.get_full_url()
        data = req.data
        decoded_data = data.decode("utf-8") if data else ""
        self.requests_log.append({
            "url": url,
            "data": decoded_data,
            "headers": req.headers,
            "timeout": timeout
        })

        if self.fail_network:
            raise socket.timeout("Network connection timed out")

        # Auth Login
        if "/api/v2/auth/login" in url:
            if self.auth_http_403:
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
            if self.fail_auth:
                return MockHTTPResponse(b"Fails.", status=200)
            # Add mock SID cookie
            ck = http.cookiejar.Cookie(
                version=0, name="SID", value="mock_session_id_12345",
                port=None, port_specified=False, domain="localhost",
                domain_specified=False, domain_initial_dot=False, path="/",
                path_specified=True, secure=False, expires=None,
                discard=True, comment=None, comment_url=None, rest={}, rfc2109=False
            )
            self.client.cj.set_cookie(ck)
            return MockHTTPResponse(b"Ok.", status=200)

        # App Version
        if "/api/v2/app/version" in url:
            return MockHTTPResponse(self.mock_version.encode("utf-8"), status=200)

        # Torrents Info
        if "/api/v2/torrents/info" in url:
            return MockHTTPResponse(json.dumps(self.mock_torrents).encode("utf-8"), status=200)

        # Torrents Files
        if "/api/v2/torrents/files" in url:
            return MockHTTPResponse(json.dumps(self.mock_files).encode("utf-8"), status=200)

        # Add / Pause / Resume / Delete / FilePrio
        if any(ep in url for ep in ["/api/v2/torrents/add", "/api/v2/torrents/pause", "/api/v2/torrents/resume", "/api/v2/torrents/delete", "/api/v2/torrents/filePrio"]):
            return MockHTTPResponse(b"Ok.", status=200)

        return MockHTTPResponse(b"Not Found", status=404)


# ======================================================================
# TIER 1: QBitClient Web API v2 Unit Tests
# ======================================================================
def test_tier_1_qbit_client():
    print("\n--- Tier 1: QBitClient Web API v2 Unit Tests ---")

    # 1. Parameter initialization
    c = QBitClient(host="localhost", port=8080, username="admin", password="password", use_ssl=False)
    check(c.port == 8080, "QBitClient initializes with integer port 8080")
    check(c.use_ssl is False, "QBitClient initializes with use_ssl=False")
    check(c.base_url == "http://localhost:8080", "QBitClient base_url synthesizes http scheme")
    check(c.category == "novels", "QBitClient default category is 'novels'")
    check(c.tags == "noveltrax", "QBitClient default tags is 'noveltrax'")

    # 2. SSL scheme selection
    c_ssl = QBitClient(host="remote.noveltrax.io", port=443, use_ssl=True)
    check(c_ssl.use_ssl is True, "SSL boolean toggle chooses True")
    check(c_ssl.base_url == "https://remote.noveltrax.io:443", "SSL configuration sets https scheme")

    # 3. Base URL initialization
    c_url = QBitClient(base_url="https://qbit.internal:9090")
    check(c_url.use_ssl is True, "base_url https initializes use_ssl=True")
    check(c_url.port == 9090, "base_url port 9090 parsed accurately")
    check(c_url.host == "qbit.internal", "base_url host parsed accurately")

    # 4. ConnectionTestResult boolean evaluation & dictionary access
    res_ok = ConnectionTestResult({"success": True, "version": "v4.6.5", "message": "Connected"})
    check(bool(res_ok) is True, "ConnectionTestResult evaluates as boolean True on success")
    check(res_ok == True, "ConnectionTestResult compares equal to True")
    check(res_ok["version"] == "v4.6.5", "ConnectionTestResult provides dictionary key access")

    res_fail = ConnectionTestResult({"success": False, "version": "", "message": "Auth failed"})
    check(bool(res_fail) is False, "ConnectionTestResult evaluates as boolean False on failure")
    check(res_fail == False, "ConnectionTestResult compares equal to False")

    # 5. Successful login & session management
    c_mock = QBitClient(host="localhost", port=8080, username="admin", password="password")
    mock_opener = MockUrllibOpener(c_mock)
    c_mock.opener = mock_opener

    login_res = c_mock.login()
    check(login_res is True, "Login returns True on valid response")
    check(c_mock.authenticated is True, "Login sets authenticated=True")
    check(any(ck.name == "SID" for ck in c_mock.cj), "CookieJar manages SID session cookie")

    # 6. Auth failure handling (403 and body 'Fails.')
    c_fail = QBitClient(host="localhost", port=8080)
    mock_fail = MockUrllibOpener(c_fail)
    mock_fail.fail_auth = True
    c_fail.opener = mock_fail
    check(c_fail.login() is False, "Login returns False when response body contains 'Fails.'")
    check(c_fail.authenticated is False, "Authenticated remains False on login failure")

    mock_fail.fail_auth = False
    mock_fail.auth_http_403 = True
    check(c_fail.login() is False, "HTTP 403 treated as authentication failure")

    # 7. Disabled downloader skipping network requests
    c_disabled = QBitClient(enabled=False)
    mock_dis = MockUrllibOpener(c_disabled)
    c_disabled.opener = mock_dis
    check(c_disabled.login() is False, "Disabled client skips login")
    check(len(mock_dis.requests_log) == 0, "Disabled client performs zero network requests")
    dis_test = c_disabled.test_connection()
    check(bool(dis_test) is False, "Disabled client test_connection returns False")
    check("disabled" in dis_test["message"].lower(), "Disabled client test_connection returns descriptive message")

    # 8. test_connection and short-term caching
    c_test = QBitClient(host="localhost", port=8080)
    mock_t = MockUrllibOpener(c_test)
    c_test.opener = mock_t

    conn_res = c_test.test_connection(force=True)
    check(conn_res.success is True, "test_connection returns success=True")
    check(conn_res["version"] == "v4.6.5", "test_connection returns version 'v4.6.5'")
    reqs_before = len(mock_t.requests_log)

    # Calling again without force uses cache
    conn_cached = c_test.test_connection(force=False, max_age=8.0)
    check(conn_cached["version"] == "v4.6.5", "Cached test_connection returns cached result")
    check(len(mock_t.requests_log) == reqs_before, "Cached test_connection makes no new network requests")

    # 9. Network timeout handling
    mock_t.fail_network = True
    conn_timeout = c_test.test_connection(force=True)
    check(conn_timeout.success is False, "test_connection handles timeout gracefully returning success=False")

    # 10. add_torrent / add_torrent_url formatting
    c_add = QBitClient(host="localhost", port=8080)
    mock_add = MockUrllibOpener(c_add)
    c_add.opener = mock_add
    c_add.authenticated = True

    magnet = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Overlord"
    add_ok = c_add.add_torrent_url(magnet, category="novels", savepath="/downloads/novels")
    check(add_ok is True, "add_torrent_url successfully submits magnet link")
    last_req = mock_add.requests_log[-1]
    check("/api/v2/torrents/add" in last_req["url"], "add_torrent_url calls /api/v2/torrents/add")
    check("category=novels" in last_req["data"], "add_torrent_url passes category=novels")
    check("savepath=%2Fdownloads%2Fnovels" in last_req["data"] or "savepath=/downloads/novels" in last_req["data"], "add_torrent_url passes savepath")

    # 11. get_torrents
    torrents_list = c_add.get_torrents(category="novels")
    check(len(torrents_list) == 1, "get_torrents returns list of active torrent dictionaries")
    check(torrents_list[0]["hash"] == "hash123", "get_torrents contains expected hash")

    # 12. Torrent controls: pause, resume, delete
    check(c_add.pause_torrent("hash123") is True, "pause_torrent succeeds")
    check("/api/v2/torrents/pause" in mock_add.requests_log[-1]["url"], "pause_torrent maps to /api/v2/torrents/pause")
    check("hashes=hash123" in mock_add.requests_log[-1]["data"], "pause_torrent passes hashes parameter")

    check(c_add.resume_torrent("hash123") is True, "resume_torrent succeeds")
    check("/api/v2/torrents/resume" in mock_add.requests_log[-1]["url"], "resume_torrent maps to /api/v2/torrents/resume")

    check(c_add.delete_torrent("hash123", delete_files=True) is True, "delete_torrent succeeds")
    check("/api/v2/torrents/delete" in mock_add.requests_log[-1]["url"], "delete_torrent maps to /api/v2/torrents/delete")
    check("deleteFiles=true" in mock_add.requests_log[-1]["data"], "delete_torrent passes deleteFiles=true")

    # 13. Hash serialization and empty list safety
    check(c_add._serialize_hashes([]) == "", "Empty hash list serializes safely as empty string")
    check(c_add._serialize_hashes(["h1", "h2"]) == "h1|h2", "Multiple hashes serialize joined with pipe (|)")
    check(c_add._serialize_hashes("single") == "single", "Single string hash serializes cleanly")

    # 14. Magnet hash extraction (40-hex and base32)
    h_hex = QBitClient.extract_torrent_hash("magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Novel")
    check(h_hex == "0123456789abcdef0123456789abcdef01234567", "extract_torrent_hash extracts standard 40-character hex hash")

    # Base32: 32 chars 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP'
    h_b32 = QBitClient.extract_torrent_hash("magnet:?xt=urn:btih:JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP&dn=B32Test")
    check(h_b32 is not None and len(h_b32) == 40, "extract_torrent_hash converts Base32 hash to 40-character hex string")

    # 15. Bare info-hashes (40-hex and 32-Base32)
    h_bare_hex = QBitClient.extract_torrent_hash("4a5b6c7d8e9f0123456789abcdef0123456789ab")
    check(h_bare_hex == "4a5b6c7d8e9f0123456789abcdef0123456789ab", "extract_torrent_hash extracts bare 40-character hex hash")

    h_bare_b32 = QBitClient.extract_torrent_hash("JJZEYZLZJJZEYZLZJJZEYZLZJJZEYZLZ")
    check(h_bare_b32 is not None and len(h_bare_b32) == 40, "extract_torrent_hash extracts and decodes bare 32-character Base32 hash")

    # 15b. Truncated and malformed magnet hashes return None
    check(
        QBitClient.extract_torrent_hash("magnet:?xt=urn:btih:4a5b6c7d8e9f0123456789abcdef0123456789a&dn=Novel") is None,
        "extract_torrent_hash returns None for truncated 39-hex hash in magnet link"
    )
    check(
        QBitClient.extract_torrent_hash("magnet:?xt=urn:btih:01234567890123456789012345678901234&dn=Novel") is None,
        "extract_torrent_hash returns None for truncated 35-hex hash in magnet link"
    )
    check(
        QBitClient.extract_torrent_hash("magnet:?xt=urn:btih:88888888888888888888888888888888&dn=Novel") is None,
        "extract_torrent_hash returns None for invalid Base32 characters in magnet link"
    )
    check(
        QBitClient.extract_torrent_hash("magnet:?xt=urn:btih:4a5b6c7d8e9f0123456789abcdef0123456789ab123&dn=Novel") is None,
        "extract_torrent_hash returns None for overflow 43-hex hash in magnet link"
    )
    check(
        QBitClient.extract_torrent_hash("4a5b6c7d8e9f0123456789abcdef0123456789a") is None,
        "extract_torrent_hash returns None for bare 39-hex hash"
    )
    check(
        QBitClient.extract_torrent_hash("JJZEYZLZJJZEYZLZJJZEYZLZJJZEYZL") is None,
        "extract_torrent_hash returns None for bare 31-char Base32 hash"
    )
    check(
        QBitClient.extract_torrent_hash("88888888888888888888888888888888") is None,
        "extract_torrent_hash returns None for bare invalid Base32 characters"
    )

    # 16. SSRF cloud metadata and destination validation
    c_meta = QBitClient(host="169.254.169.254")
    res_meta = c_meta.test_connection(force=True)
    check(res_meta.success is False and "cloud metadata" in res_meta["message"].lower(), "SSRF guard blocks link-local cloud metadata IP in test_connection")

    c_gcp = QBitClient(base_url="http://metadata.google.internal:8080")
    res_gcp = c_gcp.test_connection(force=True)
    check(res_gcp.success is False and "cloud metadata" in res_gcp["message"].lower(), "SSRF guard blocks cloud metadata hostname in test_connection")

    c_ftp = QBitClient(base_url="ftp://localhost:8080")
    res_ftp = c_ftp.test_connection(force=True)
    check(res_ftp.success is False and "scheme" in res_ftp["message"].lower(), "SSRF guard blocks non-http scheme in test_connection")

    # Destination validator matrix (loopback & LAN allowed, metadata blocked)
    check(validate_downloader_destination("127.0.0.1")[0] is True, "validate_downloader_destination permits 127.0.0.1 loopback")
    check(validate_downloader_destination("localhost")[0] is True, "validate_downloader_destination permits localhost loopback")
    check(validate_downloader_destination("192.168.1.100")[0] is True, "validate_downloader_destination permits RFC 1918 192.168.x.x LAN")
    check(validate_downloader_destination("10.0.0.5")[0] is True, "validate_downloader_destination permits RFC 1918 10.x.x.x LAN")
    check(validate_downloader_destination("169.254.169.254")[0] is False, "validate_downloader_destination strictly blocks 169.254.169.254")
    check(validate_downloader_destination("metadata.google.internal")[0] is False, "validate_downloader_destination strictly blocks metadata.google.internal")


# ======================================================================
# TIER 2: Light Novel Filename Parsing & Volume Extraction Tests
# ======================================================================
def test_tier_2_filename_parsing():
    print("\n--- Tier 2: Light Novel Filename Parsing & Volume Extraction Tests ---")

    # 1. Single volume with bracket group
    is_anc, v1 = parse_torrent_filename_volumes("[Yen Press] Overlord - Vol. 01 [Digital].epub")
    check(is_anc is False and v1 == {1.0}, "Parses single volume with group tag: '[Yen Press] Overlord - Vol. 01 [Digital].epub' -> {1.0}")

    # 2. Volume keyword
    is_anc, v2 = parse_torrent_filename_volumes("Classroom of the Elite Volume 15.epub")
    check(is_anc is False and v2 == {15.0}, "Parses explicit 'Volume 15' -> {15.0}")

    # 3. v-prefix
    is_anc, v3 = parse_torrent_filename_volumes("Mushoku Tensei v08.azw3")
    check(is_anc is False and v3 == {8.0}, "Parses compact 'v08.azw3' -> {8.0}")

    # 4. Decimal volume
    is_anc, v4 = parse_torrent_filename_volumes("[Seven Seas] COTE - Year 2 Vol 04.5.epub")
    check(is_anc is False and v4 == {4.5}, "Parses decimal volume 'Vol 04.5' -> {4.5}")

    # 5. Multi-volume omnibus range
    is_anc, v5 = parse_torrent_filename_volumes("Overlord Vol 01-03.epub")
    check(is_anc is False and v5 == {1.0, 2.0, 3.0}, "Parses omnibus range 'Vol 01-03' -> {1.0, 2.0, 3.0}")

    # 6. Bracketed omnibus range
    is_anc, v6 = parse_torrent_filename_volumes("Sword Art Online [Vol 1-4].epub")
    check(is_anc is False and v6 == {1.0, 2.0, 3.0, 4.0}, "Parses bracketed range '[Vol 1-4]' -> {1.0, 2.0, 3.0, 4.0}")

    # 7. Bare number after delimiter
    is_anc, v7 = parse_torrent_filename_volumes("That Time I Got Reincarnated as a Slime - 02.epub")
    check(is_anc is False and v7 == {2.0}, "Parses bare volume after hyphen ' - 02.epub' -> {2.0}")

    # 8. Numbered series guard: '86 - Eighty Six'
    is_anc, v8 = parse_torrent_filename_volumes("86 - Eighty Six - Vol 01.epub", series_name="86")
    check(is_anc is False and v8 == {1.0}, "Avoids matching '86' in '86 - Eighty Six - Vol 01.epub', extracting {1.0}")

    # 9. Numbered series guard: 'Mob Psycho 100'
    is_anc, v9 = parse_torrent_filename_volumes("Mob Psycho 100 - Vol 03.epub", series_name="Mob Psycho 100")
    check(is_anc is False and v9 == {3.0}, "Avoids matching '100' in 'Mob Psycho 100 - Vol 03.epub', extracting {3.0}")

    # 10. Ancillary clutter files
    for fname in ["series.nfo", "cover.jpg", "readme.txt", "download_link.url", "checksum.sfv"]:
        is_anc, _ = parse_torrent_filename_volumes(fname)
        check(is_anc is True, f"Correctly flags ancillary non-book file '{fname}'")

    # 11. Nested subdirectory paths
    is_anc, v11 = parse_torrent_filename_volumes("Series Name/Volume 05/novel.epub")
    check(is_anc is False and v11 == {5.0}, "Extracts volume from nested subdirectory path 'Volume 05/novel.epub' -> {5.0}")

    # 12. Numbered series with release group tags
    is_anc, v_tag1 = parse_torrent_filename_volumes("[Yen Press] Mob Psycho 100 - Vol 03.epub")
    check(is_anc is False and v_tag1 == {3.0}, "Parses numbered series with leading release tag: '[Yen Press] Mob Psycho 100 - Vol 03.epub' -> {3.0}")

    is_anc, v_tag2 = parse_torrent_filename_volumes("[Group] 86 - Vol 01.epub")
    check(is_anc is False and v_tag2 == {1.0}, "Parses numbered series with leading group tag: '[Group] 86 - Vol 01.epub' -> {1.0}")

    is_anc, v_tag3 = parse_torrent_filename_volumes("[J-Novel Club] Mob Psycho 100 - Vol 02.epub", series_name="Mob Psycho 100")
    check(is_anc is False and v_tag3 == {2.0}, "Parses series_name with leading group tag: '[J-Novel Club] Mob Psycho 100 - Vol 02.epub' -> {2.0}")

    is_anc, v_num_range = parse_torrent_filename_volumes("Catch-22 - Vol 1-3.epub")
    check(is_anc is False and v_num_range == {1.0, 2.0, 3.0}, "Parses numbered series with volume range: 'Catch-22 - Vol 1-3.epub' -> {1.0, 2.0, 3.0}")

    is_anc, v_bare_86 = parse_torrent_filename_volumes("86 - Vol 01.epub")
    check(is_anc is False and v_bare_86 == {1.0}, "Parses unadorned numbered series without series_name: '86 - Vol 01.epub' -> {1.0}")

    # 13. Multi-volume conjunctions and lists
    is_anc, v_conj1 = parse_torrent_filename_volumes("Mushoku Tensei Vol 1 & 2.epub")
    check(is_anc is False and v_conj1 == {1.0, 2.0}, "Parses conjunction '&' volume list: 'Mushoku Tensei Vol 1 & 2.epub' -> {1.0, 2.0}")

    is_anc, v_conj2 = parse_torrent_filename_volumes("Re:Zero Vol 1 + 2.epub")
    check(is_anc is False and v_conj2 == {1.0, 2.0}, "Parses plus '+' volume list: 'Re:Zero Vol 1 + 2.epub' -> {1.0, 2.0}")

    is_anc, v_conj3 = parse_torrent_filename_volumes("Konosuba Vol 1 and 2.epub")
    check(is_anc is False and v_conj3 == {1.0, 2.0}, "Parses word 'and' volume list: 'Konosuba Vol 1 and 2.epub' -> {1.0, 2.0}")

    is_anc, v_conj4 = parse_torrent_filename_volumes("Overlord Vol 1, 2, 3.epub")
    check(is_anc is False and v_conj4 == {1.0, 2.0, 3.0}, "Parses comma volume list: 'Overlord Vol 1, 2, 3.epub' -> {1.0, 2.0, 3.0}")

    # 13b. Oxford comma lists and conjunction combinations
    is_anc, v_ox1 = parse_torrent_filename_volumes("DanMachi Vol 1, 2, and 3.epub")
    check(is_anc is False and v_ox1 == {1.0, 2.0, 3.0}, "Parses Oxford comma list with 'and': 'DanMachi Vol 1, 2, and 3.epub' -> {1.0, 2.0, 3.0}")

    is_anc, v_ox2 = parse_torrent_filename_volumes("Overlord v1, 2, 3, and 4.epub")
    check(is_anc is False and v_ox2 == {1.0, 2.0, 3.0, 4.0}, "Parses 4-item Oxford comma list: 'Overlord v1, 2, 3, and 4.epub' -> {1.0, 2.0, 3.0, 4.0}")

    is_anc, v_ox3 = parse_torrent_filename_volumes("Ascendance of a Bookworm Books 1, 2, & 3.epub")
    check(is_anc is False and v_ox3 == {1.0, 2.0, 3.0}, "Parses Oxford comma list with '&': 'Ascendance of a Bookworm Books 1, 2, & 3.epub' -> {1.0, 2.0, 3.0}")

    is_anc, v_ox4 = parse_torrent_filename_volumes("Slime Volume 1, 2, + 3.epub")
    check(is_anc is False and v_ox4 == {1.0, 2.0, 3.0}, "Parses Oxford comma list with '+': 'Slime Volume 1, 2, + 3.epub' -> {1.0, 2.0, 3.0}")

    # 14. Fractional range lower bound stepping
    is_anc, v_frac = parse_torrent_filename_volumes("Series Vol 1.5-3.epub")
    check(is_anc is False and v_frac == {1.5, 2.0, 3.0} and 1.0 not in v_frac, "Fractional range Vol 1.5-3 does not contain lower integer 1.0")

    # 15. Oversized omnibus range safety fallback
    is_anc, v_over = parse_torrent_filename_volumes("Guin Saga Vol 1-120.epub")
    check(is_anc is False and v_over == set(), "Oversized omnibus range (>100 vols) safely returns empty set to keep file")


# ======================================================================
# TIER 3: Selective Volume Downloading Algorithm Tests
# ======================================================================
def test_tier_3_selective_downloading():
    print("\n--- Tier 3: Selective Volume Downloading Algorithm Tests ---")

    c = QBitClient(host="localhost", port=8080)
    mock_opener = MockUrllibOpener(c)
    c.opener = mock_opener
    c.authenticated = True

    # 1. Normal multi-file filtering: Torrent has Vols 1-5; user owns Vols 1-3
    mock_opener.mock_files = [
        {"id": 0, "name": "Overlord - Vol 01.epub", "size": 5000000},
        {"id": 1, "name": "Overlord - Vol 02.epub", "size": 5000000},
        {"id": 2, "name": "Overlord - Vol 03.epub", "size": 5000000},
        {"id": 3, "name": "Overlord - Vol 04.epub", "size": 5000000},
        {"id": 4, "name": "Overlord - Vol 05.epub", "size": 5000000},
    ]

    res = c.apply_selective_volume_download("h_normal", owned_volumes={1.0, 2.0, 3.0})
    check(res["status"] == "filtered", "Status is 'filtered' when subset of volumes is owned")
    check(res.skipped_files == 3, "Skipped files count is 3")
    check(int(res) == 3, "SelectiveDownloadResult int() coercion returns 3")
    check(res["skipped_indices"] == [0, 1, 2], "Files 0, 1, 2 marked for deselection")
    check(res["kept_indices"] == [3, 4], "Files 3, 4 kept for active download")
    check(res["skipped_volumes"] == [1.0, 2.0, 3.0], "Skipped volumes list contains [1.0, 2.0, 3.0]")
    check(res["kept_volumes"] == [4.0, 5.0], "Kept volumes list contains [4.0, 5.0]")

    # 2. Omnibus subset rule:
    # Torrent has File 0 (Vol 01-03.epub) and File 1 (Vol 04.epub)
    # User owns Vols 1, 2, 4 (missing Vol 3!)
    mock_opener.mock_files = [
        {"id": 0, "name": "DanMachi Vol 01-03.epub", "size": 15000000},
        {"id": 1, "name": "DanMachi Vol 04.epub", "size": 5000000},
    ]

    res_omni = c.apply_selective_volume_download("h_omni", owned_volumes={1.0, 2.0, 4.0})
    check(res_omni["status"] == "filtered", "Omnibus partial bundle status is 'filtered'")
    check(res_omni["kept_indices"] == [0], "Omnibus file Vol 01-03 is KEPT because Volume 3 is missing")
    check(res_omni["skipped_indices"] == [1], "Single file Vol 04 is SKIPPED because Volume 4 is owned")

    # 3. Omnibus all owned
    res_omni_all = c.apply_selective_volume_download("h_omni_all", owned_volumes={1.0, 2.0, 3.0, 4.0})
    check(res_omni_all["status"] == "all_owned", "Omnibus file is SKIPPED when ALL contained volumes {1,2,3} are owned")
    check(res_omni_all["skipped_indices"] == [0, 1], "All files deselected when all volumes owned")

    # 4. All needed (user owns none)
    mock_opener.requests_log.clear()
    res_needed = c.apply_selective_volume_download("h_needed", owned_volumes=set())
    check(res_needed["status"] == "all_needed", "Status is 'all_needed' when user owns no volumes")
    check(res_needed.skipped_files == 0, "Zero files skipped when all needed")
    check(len([r for r in mock_opener.requests_log if "/filePrio" in r["url"]]) == 0, "No filePrio HTTP POST requests made when all needed")

    # 5. Single monolithic archive
    mock_opener.mock_files = [
        {"id": 0, "name": "Complete_Overlord_Series.zip", "size": 80000000}
    ]
    res_single = c.apply_selective_volume_download("h_single", owned_volumes={1.0})
    check(res_single["status"] == "single_archive", "Status is 'single_archive' for monolithic archive")
    check(res_single.skipped_files == 0, "Zero files skipped for single archive")

    # 6. Pending metadata (empty file list from DHT)
    mock_opener.mock_files = []
    res_pending = c.apply_selective_volume_download("h_pending", owned_volumes={1.0})
    check(res_pending["status"] == "pending_metadata", "Status is 'pending_metadata' when file list is empty")
    check(res_pending.skipped_files == 0, "Zero files skipped when metadata pending")

    # 7. Ancillary clutter file deselection
    mock_opener.mock_files = [
        {"id": 0, "name": "DanMachi Vol 19.epub", "size": 6000000},
        {"id": 1, "name": "DanMachi Vol 20.epub", "size": 6000000},
        {"id": 2, "name": "Cover.jpg", "size": 500000},
        {"id": 3, "name": "release_info.nfo", "size": 2000},
    ]
    res_anc = c.apply_selective_volume_download("h_anc", owned_volumes={19.0})
    check(0 in res_anc["skipped_indices"], "Owned Vol 19 is skipped")
    check(1 in res_anc["kept_indices"], "Missing Vol 20 is kept")
    check(2 in res_anc["skipped_indices"], "Cover.jpg ancillary file is skipped")
    check(3 in res_anc["skipped_indices"], "release_info.nfo ancillary file is skipped")

    # 8. wanted_volumes alternative parameter interface
    res_wanted = c.apply_selective_volume_download("h_wanted", wanted_volumes=[20.0])
    check(0 in res_wanted["skipped_indices"], "Vol 19 not in wanted_volumes is skipped")
    check(1 in res_wanted["kept_indices"], "Vol 20 in wanted_volumes is kept")

    # 9. Selective download behavior with partial owned sets and Oxford comma files
    mock_opener.mock_files = [
        {"id": 0, "name": "Mushoku Tensei Vol 1 & 2.epub", "size": 6000000},
        {"id": 1, "name": "Overlord v1, 2, 3.epub", "size": 9000000},
        {"id": 2, "name": "DanMachi Vol 1, 2, and 3.epub", "size": 10000000},
        {"id": 3, "name": "Ascendance of a Bookworm Books 1, 2, & 3.epub", "size": 9000000},
        {"id": 4, "name": "Slime Volume 1, 2, + 3.epub", "size": 9000000},
    ]
    # Scenario: User owns {1.0, 2.0} -> Needs Volume 3.0!
    # File 0: Vol 1 & 2 -> Fully owned {1.0, 2.0} -> must be SKIPPED
    # File 1: v1, 2, 3 -> Partially owned (needs 3.0) -> must be KEPT
    # File 2: Vol 1, 2, and 3 -> Partially owned (needs 3.0) -> must be KEPT
    # File 3: Books 1, 2, & 3 -> Partially owned (needs 3.0) -> must be KEPT
    # File 4: Volume 1, 2, + 3 -> Partially owned (needs 3.0) -> must be KEPT
    res_ox_sel = c.apply_selective_volume_download("h_ox_sel", owned_volumes={1.0, 2.0})
    check(res_ox_sel["status"] == "filtered", "Partial owned set selective download status is 'filtered'")
    check(res_ox_sel.skipped_files == 1, "Exactly 1 fully owned file (Vol 1 & 2) skipped")
    check(0 in res_ox_sel["skipped_indices"], "File 0 (Vol 1 & 2) skipped because user owns {1.0, 2.0}")
    check(1 in res_ox_sel["kept_indices"], "File 1 (v1, 2, 3) kept because user needs Volume 3.0")
    check(2 in res_ox_sel["kept_indices"], "File 2 ('DanMachi Vol 1, 2, and 3') kept because user needs Volume 3.0")
    check(3 in res_ox_sel["kept_indices"], "File 3 ('Books 1, 2, & 3') kept because user needs Volume 3.0")
    check(4 in res_ox_sel["kept_indices"], "File 4 ('Volume 1, 2, + 3') kept because user needs Volume 3.0")


# ======================================================================
# TIER 4: Downloader REST API & UI View Verification Tests
# ======================================================================
def test_tier_4_api_and_ui_views():
    print("\n--- Tier 4: Downloader REST API & UI View Verification Tests ---")

    # 1. downloads.html template existence and required content
    tpl_path = os.path.join(BACKEND_DIR, "app", "templates", "downloads.html")
    check(os.path.isfile(tpl_path), "backend/app/templates/downloads.html exists")
    with open(tpl_path, "r", encoding="utf-8") as f:
        src = f.read()

    check("Downloader & Queue Management" in src, "downloads.html renders page header")
    for col in ["Name", "Size", "Progress", "Speed", "Status", "Actions"]:
        check(f">{col}<" in src or f"th>{col}<" in src or col in src, f"downloads.html contains column header '{col}'")

    for btn_cls in ["btn-pause", "btn-resume", "btn-delete"]:
        check(btn_cls in src, f"downloads.html includes control button class '{btn_cls}'")

    for field in ["qbit_url", "qbit_username", "qbit_password", "qbit_category"]:
        check(f'id="{field}"' in src or f'name="{field}"' in src, f"downloads.html contains settings input '{field}'")

    # 2. base.html navigation link
    base_path = os.path.join(BACKEND_DIR, "app", "templates", "base.html")
    with open(base_path, "r", encoding="utf-8") as f:
        base_src = f.read()
    check('href="/downloads"' in base_src, "base.html contains navigation link to /downloads")
    check('data-lucide="download-cloud"' in base_src or 'data-lucide="download"' in base_src, "base.html navbar link uses download icon")

    # 3. Database migrations & settings defaults
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_db.close()
    try:
        db = Database(tmp_db.name)
        settings = db.get_all_settings()
        for k in ["qbit_enabled", "qbit_host", "qbit_port", "qbit_username", "qbit_password", "qbit_use_ssl", "qbit_category", "qbit_savepath", "qbit_url"]:
            check(k in settings, f"Database.init_db seeds setting '{k}'")

        # Releases table columns
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(releases);")
            col_names = [row["name"] for row in cur.fetchall()]
            check("download_url" in col_names, "releases table contains 'download_url' column")
            check("download_status" in col_names, "releases table contains 'download_status' column")
            check("torrent_hash" in col_names, "releases table contains 'torrent_hash' column")

        # 4. Database update_download_status & get_release_by_id
        ok, r_id = db.upsert_release({
            "guid": "test_m3_guid_1",
            "title": "DanMachi Vol 19 [EPUB]",
            "series_name": "DanMachi",
            "volume": 19.0,
            "link": "magnet:?xt=urn:btih:da39a3ee5e6b4b0d3255bfef95601890afd80709"
        })
        check(ok is True, "Release inserted successfully")

        r_before = db.get_release_by_id(r_id)
        check(r_before["download_status"] == "IDLE", "Initial download_status is 'IDLE'")

        db.update_download_status(r_id, "DOWNLOADING", "da39a3ee5e6b4b0d3255bfef95601890afd80709")
        r_after = db.get_release_by_id(r_id)
        check(r_after["download_status"] == "DOWNLOADING", "update_download_status sets status to 'DOWNLOADING'")
        check(r_after["torrent_hash"] == "da39a3ee5e6b4b0d3255bfef95601890afd80709", "update_download_status sets torrent_hash")

    finally:
        try:
            os.unlink(tmp_db.name)
        except OSError:
            pass

    # 5. FastAPI route definitions in main.py
    main_path = os.path.join(BACKEND_DIR, "app", "main.py")
    with open(main_path, "r", encoding="utf-8") as f:
        main_src = f.read()

    expected_routes = [
        '@app.get("/downloads"',
        '@app.get("/api/downloader/queue"',
        '@app.get("/api/downloads"',
        '@app.get("/api/downloads/status"',
        '@app.post("/api/downloader/test"',
        '@app.post("/api/downloader/configure"',
        '@app.post("/api/downloader/pause/{torrent_hash}"',
        '@app.post("/api/downloader/resume/{torrent_hash}"',
        '@app.delete("/api/downloader/delete/{torrent_hash}"',
        '@app.post("/api/downloads/{torrent_hash}/selective"',
        '@app.post("/api/releases/{release_id}/download"',
    ]
    for route in expected_routes:
        check(route in main_src, f"main.py defines endpoint {route}")


# ======================================================================
# TIER 5: Calibre Cross-Reference & End-to-End Selective Download Pipeline
# ======================================================================
def test_tier_5_e2e_pipeline():
    print("\n--- Tier 5: Calibre Cross-Reference & End-to-End Pipeline Tests ---")

    # 1. Calibre omnibus library mapping driving selective download
    # User owns Overlord Vols 1 to 14 in Calibre (expanded via omnibus)
    calibre_owned_map = {"overlord": {float(i) for i in range(1, 15)}}

    # An incoming torrent bundle arrives containing Overlord Vols 1 to 16
    torrent_bundle_files = [
        {"id": i, "name": f"Overlord - Vol {i:02d}.epub"}
        for i in range(1, 17)
    ]

    c = QBitClient(host="localhost", port=8080)
    mock_opener = MockUrllibOpener(c)
    mock_opener.mock_files = torrent_bundle_files
    c.opener = mock_opener
    c.authenticated = True

    owned_vols = calibre_owned_map.get("overlord", set())
    res = c.apply_selective_volume_download("hash_overlord_bundle", owned_volumes=owned_vols)

    check(res["status"] == "filtered", "Pipeline status is 'filtered'")
    check(res.skipped_files == 14, "Correctly skipped 14 owned volume files")
    check(res["kept_files"] == 2, "Kept exactly 2 missing volume files (Vols 15 & 16)")
    check(res["kept_volumes"] == [15.0, 16.0], "Kept volumes are exactly missing volumes [15.0, 16.0]")

    # 2. End-to-end release download & selective priority workflow
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_db.close()
    try:
        db = Database(tmp_db.name)
        # Release arrives for DanMachi Vols 18-20 (user owns Vol 18, needs 19 and 20)
        magnet = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=DanMachi_Bundle"
        ok, r_id = db.upsert_release({
            "guid": "danmachi_bundle_guid",
            "title": "DanMachi Vol 18-20 Epub",
            "series_name": "DanMachi",
            "volume": None,
            "link": magnet,
            "is_bundle": 1,
            "volumes": [18.0, 19.0, 20.0],
            "missing_volumes": [19.0, 20.0],
            "volumes_json": json.dumps([18.0, 19.0, 20.0]),
            "missing_vols_json": json.dumps([19.0, 20.0])
        })
        check(ok is True, "Bundle release inserted into database")

        # Simulate release download action:
        target = db.get_release_by_id(r_id)
        download_url = target.get("download_url") or target.get("link")
        torrent_h = c.extract_torrent_hash(download_url)
        check(torrent_h == "0123456789abcdef0123456789abcdef01234567", "Extracted torrent hash matches magnet BTIH")

        # Add torrent to downloader
        add_ok = c.add_torrent_url(download_url, category="novels", savepath="/downloads")
        check(add_ok is True, "Torrent successfully submitted to client")

        # Update database download status
        db.update_download_status(r_id, "DOWNLOADING", torrent_h)
        rel_down = db.get_release_by_id(r_id)
        check(rel_down["download_status"] == "DOWNLOADING", "Release status updated to 'DOWNLOADING'")
        check(rel_down["torrent_hash"] == torrent_h, "Release torrent_hash persisted")

        # Apply selective volume filtering for bundle
        mock_opener.mock_files = [
            {"id": 0, "name": "DanMachi Vol 18.epub"},
            {"id": 1, "name": "DanMachi Vol 19.epub"},
            {"id": 2, "name": "DanMachi Vol 20.epub"},
        ]
        missing_vols = json.loads(target["missing_vols_json"])
        filter_res = c.apply_selective_volume_download(torrent_h, wanted_volumes=missing_vols)
        check(filter_res.skipped_files == 1, "Selective filter skipped owned Volume 18")
        check(filter_res["kept_indices"] == [1, 2], "Selective filter kept missing Volumes 19 & 20")

    finally:
        try:
            os.unlink(tmp_db.name)
        except OSError:
            pass


def main():
    print("======================================================================")
    print("  RUNNING MILESTONE 3: DOWNLOADER & QUEUE MANAGEMENT TEST SUITE")
    print("======================================================================")

    test_tier_1_qbit_client()
    test_tier_2_filename_parsing()
    test_tier_3_selective_downloading()
    test_tier_4_api_and_ui_views()
    test_tier_5_e2e_pipeline()

    print("======================================================================")
    print(f"  ALL {passed_count} MILESTONE 3 TESTS PASSED WITH 100% SUCCESS! (Failed: {failed_count})")
    print("======================================================================")


if __name__ == "__main__":
    main()
