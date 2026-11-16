"""Regression guards for the November 2026 hardening batch (NT-01 … NT-09).

Every check here maps to a finding confirmed by exploitation during the audit, so a
re-appearance fails loudly:

NT-01 SSRF + response oracle via an unvalidated EPUB fetcher URL
NT-02 plaintext qBittorrent password rendered into /downloads
NT-03 forked cover SSRF guard weaker than app.security (CGNAT reachable)
NT-04 stored ``javascript:`` URL rendered as a clickable href
NT-06 safe_http_get byte ceiling enforced only after the whole body arrived
NT-08 compose publishing the unauthenticated API on every interface
NT-09 unpinned dependency ranges
"""

import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_BASE = os.path.dirname(_HERE)
for _p in (_BASE, os.path.join(_BASE, "backend"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Point the app at a throwaway data dir before importing it.
_DATA_DIR = tempfile.mkdtemp(prefix="nt_hardening_")
os.environ["DATA_DIR"] = _DATA_DIR
os.environ.setdefault("CALIBRE_DIR", _DATA_DIR)
os.environ.setdefault("DOWNLOAD_DIR", os.path.join(_DATA_DIR, "downloads"))

import urllib.request  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app import covers, security  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def _client():
    return TestClient(app_main.app)


def _make_release(guid, source_link="https://example.com/book.epub"):
    with app_main.db.get_connection() as conn:
        cur = conn.execute(
            "INSERT OR REPLACE INTO releases (guid, title, series_name, volume, source_link, link, status)"
            " VALUES (?, 'Hardening Probe Vol 1', 'Hardening Probe', 1, ?, 'https://example.com/post', 'REQUESTED');",
            (guid, source_link),
        )
        conn.commit()
        return cur.lastrowid


def test_epub_fetcher_url_rejects_metadata_destination():
    """NT-01: the sidecar URL is a server-side POST target and follows the downloader policy."""
    client = _client()
    for bad in (
        "http://169.254.169.254",
        "http://metadata.google.internal",
        "file:///etc/passwd",
        "gopher://internal:70",
    ):
        res = client.post("/api/settings", json={"epub_fetcher_url": bad})
        check(res.status_code == 400, f"epub_fetcher_url {bad!r} was accepted: {res.status_code}")
        stored = app_main.db.get_setting("epub_fetcher_url", "")
        check(stored != bad, f"epub_fetcher_url {bad!r} reached the database")
    # Loopback stays allowed: the sidecar normally runs beside the app.
    res = client.post("/api/settings", json={"epub_fetcher_url": "http://127.0.0.1:8010"})
    check(res.status_code == 200, f"loopback sidecar URL rejected: {res.status_code} {res.text[:200]}")


def test_fetch_epub_never_posts_to_a_stored_metadata_url():
    """NT-01: a value written straight into SQLite is re-validated before the POST."""
    app_main.db.set_setting("epub_fetcher_url", "http://169.254.169.254")
    release_id = _make_release("hardening-nt01")
    calls = []

    def _record(req, *args, **kwargs):
        calls.append(getattr(req, "full_url", str(req)))
        raise AssertionError("outbound request must not happen for a blocked destination")

    client = _client()
    with patch.object(urllib.request, "urlopen", side_effect=_record):
        res = client.post(f"/api/releases/{release_id}/fetch-epub")
    check(res.status_code == 400, f"expected 400 for blocked fetcher URL, got {res.status_code}: {res.text[:200]}")
    check(not calls, f"server contacted the blocked destination: {calls}")
    check("169.254.169.254" not in res.text, "rejection message must not echo the internal destination body")
    app_main.db.set_setting("epub_fetcher_url", "")


def test_fetch_epub_error_does_not_reflect_remote_body():
    """NT-01: an HTTP error from whatever answers on that URL yields status only."""
    app_main.db.set_setting("epub_fetcher_url", "http://127.0.0.1:8010")
    release_id = _make_release("hardening-nt01b")
    body = b'{"detail":"INTERNAL-ORACLE: secret-token-abc"}'

    def _raise(req, *args, **kwargs):
        raise urllib.error.HTTPError(getattr(req, "full_url", ""), 404, "Not Found", {}, _BytesReader(body))

    client = _client()
    with patch.object(urllib.request, "urlopen", side_effect=_raise):
        res = client.post(f"/api/releases/{release_id}/fetch-epub")
    check(res.status_code == 502, f"expected 502, got {res.status_code}")
    check("INTERNAL-ORACLE" not in res.text, f"remote body reflected to the caller: {res.text[:300]}")
    check("HTTP 404" in res.text, f"status code should still be reported: {res.text[:200]}")
    app_main.db.set_setting("epub_fetcher_url", "")


class _BytesReader:
    def __init__(self, payload):
        self._payload = payload

    def read(self, *_args):
        return self._payload


def test_downloads_page_never_echoes_the_qbit_password():
    """NT-02: the dashboard is unauthenticated (SEC-07), so stored secrets stay server-side."""
    app_main.db.set_setting("torbox_api_key", "TORBOX-SECRET-123")
    app_main.db.set_setting("qbit_password", "QBIT-SECRET-456")
    client = _client()
    body = client.get("/downloads").text
    check("QBIT-SECRET-456" not in body, "qBittorrent password rendered into /downloads")
    check("TORBOX-SECRET-123" not in body, "TorBox API key rendered into /downloads")
    check('id="qbit_password"' in body, "password field disappeared from /downloads")
    settings_body = client.get("/settings").text
    check("QBIT-SECRET-456" not in settings_body, "qBittorrent password rendered into /settings")


def test_downloader_configure_keeps_saved_password_and_validates_both_fields():
    """NT-02/NT-12: blank means keep; host and URL are both validated."""
    app_main.db.set_setting("qbit_password", "ORIGINAL-PASS")
    client = _client()
    with patch.object(app_main, "get_qbit_client") as fake_factory:
        fake_factory.return_value.test_connection.return_value = {"success": False, "version": "", "message": "offline"}
        ok = client.post("/api/downloader/configure", json={
            "qbit_host": "localhost", "qbit_port": 8080, "qbit_password": "",
        })
    check(ok.status_code == 200, f"blank password save failed: {ok.status_code} {ok.text[:200]}")
    check(app_main.db.get_setting("qbit_password", "") == "ORIGINAL-PASS",
          "blank password wiped the saved credential")

    with patch.object(app_main, "get_qbit_client") as fake_factory:
        fake_factory.return_value.test_connection.return_value = {"success": False, "version": "", "message": "offline"}
        rejected = client.post("/api/downloader/configure", json={
            "qbit_url": "http://127.0.0.1:8080",
            "qbit_host": "169.254.169.254",
        })
    check(rejected.status_code == 400, "unchecked qbit_host was persisted alongside a valid qbit_url")
    check(app_main.db.get_setting("qbit_host", "") != "169.254.169.254", "metadata host persisted")


def test_cover_guard_is_the_central_guard():
    """NT-03: one SSRF policy, not a weaker fork."""
    check(covers.is_safe_public_url is security.is_safe_public_url,
          "covers.py defines its own SSRF guard instead of importing app.security")
    for blocked in (
        "http://100.64.0.1/cover.jpg",          # CGNAT / Tailscale range
        "http://169.254.169.254/cover.jpg",
        "http://127.0.0.1%00@example.com/x.jpg",
        "http://example.com#@127.0.0.1/x.jpg",
        "file:///etc/passwd",
    ):
        check(security.is_safe_public_url(blocked) is False, f"central guard accepted {blocked}")
        check(covers.is_safe_public_url(blocked) is False, f"cover guard accepted {blocked}")


def test_stored_javascript_link_is_rejected_and_not_rendered():
    """NT-04: scheme allow-list on write and on render."""
    check(security.sanitize_href("javascript:alert(1)") == "", "sanitize_href accepted javascript:")
    check(security.sanitize_href("data:text/html,<script>alert(1)</script>") == "", "sanitize_href accepted data:")
    check(security.sanitize_href("file:///etc/passwd") == "", "sanitize_href accepted file:")
    check(security.sanitize_href("/relative/path") == "", "sanitize_href accepted a relative URL")
    check(security.sanitize_href("https://example.com/book") == "https://example.com/book",
          "sanitize_href rejected a normal https URL")

    client = _client()
    with app_main.db.get_connection() as conn:
        cur = conn.execute("INSERT INTO series_tracking (name) VALUES ('Hardening Series');")
        series_id = cur.lastrowid
        conn.commit()
    res = client.post(f"/api/series/{series_id}/link", json={"website_url": "javascript:alert(document.domain)"})
    check(res.status_code == 400, f"javascript: website_url accepted: {res.status_code}")

    # A row written directly into SQLite (or by an older build) must not render either.
    with app_main.db.get_connection() as conn:
        conn.execute("UPDATE series_tracking SET website_url = ? WHERE id = ?;",
                     ("javascript:alert(1)", series_id))
        conn.commit()
    body = client.get("/series").text
    check('href="javascript:' not in body, "stored javascript: URL rendered as a link")


def test_safe_http_get_aborts_before_buffering_the_whole_body():
    """NT-06: the byte ceiling is enforced while streaming, not after the download."""
    chunks = [b"X" * 40 for _ in range(1000)]
    delivered = [0]

    class FakeStream:
        status_code = 200
        is_redirect = False
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def iter_bytes(self):
            for chunk in chunks:
                delivered[0] += 1
                yield chunk

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def stream(self, method, url):
            return FakeStream()

        def close(self):
            pass

    class FakeHttpx:
        Client = FakeClient

    with patch.object(security, "httpx", FakeHttpx):
        try:
            security.safe_http_get("http://1.1.1.1/stream", max_bytes=100)
            raise AssertionError("oversized streamed body was not rejected")
        except ValueError as exc:
            check("exceeds maximum allowed size" in str(exc), f"unexpected message: {exc}")
    check(delivered[0] <= 4, f"read {delivered[0]} chunks past a 100-byte ceiling of 40-byte chunks")


def test_compose_defaults_to_loopback_and_requirements_are_pinned():
    """NT-08/NT-09: safe default exposure and reproducible builds."""
    for name in ("docker-compose.yml", "docker-compose.truenas.yml"):
        path = os.path.join(_BASE, name)
        with open(path, "r", encoding="utf-8-sig") as handle:
            text = handle.read()
        check('"8080:8080"' not in text, f"{name} still publishes the unauthenticated API on all interfaces")
        check("${NOVELTRAX_BIND:-127.0.0.1}" in text, f"{name} does not default the port to loopback")

    with open(os.path.join(_BASE, "backend", "requirements.txt"), "r", encoding="utf-8-sig") as handle:
        lines = [line.strip() for line in handle if line.strip() and not line.startswith("#")]
    check(len(lines) == 7, f"unexpected dependency count: {lines}")
    for line in lines:
        check("==" in line and ">=" not in line, f"unpinned dependency: {line}")


def main():
    test_epub_fetcher_url_rejects_metadata_destination()
    test_fetch_epub_never_posts_to_a_stored_metadata_url()
    test_fetch_epub_error_does_not_reflect_remote_body()
    test_downloads_page_never_echoes_the_qbit_password()
    test_downloader_configure_keeps_saved_password_and_validates_both_fields()
    test_cover_guard_is_the_central_guard()
    test_stored_javascript_link_is_rejected_and_not_rendered()
    test_safe_http_get_aborts_before_buffering_the_whole_body()
    test_compose_defaults_to_loopback_and_requirements_are_pinned()
    print("security hardening suite PASSED")


if __name__ == "__main__":
    main()
