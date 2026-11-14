"""Unit checks for the EPUB fetcher helpers (no browser/network required)."""

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BASE = os.path.dirname(_HERE)
for _p in (_BASE, os.path.join(_BASE, "backend"), os.path.join(_BASE, "tools", "epub_fetcher")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import epub_fetcher  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def test_book_archive_signatures():
    check(epub_fetcher._is_book_bytes(b"PK\x03\x04rest"), "epub/zip")
    check(epub_fetcher._is_book_bytes(b"%PDF-1.7"), "pdf")
    check(not epub_fetcher._is_book_bytes(b"MZ\x90\x00"), "windows exe rejected")
    check(not epub_fetcher._is_book_bytes(b"<html>"), "html rejected")


def test_executable_download_rejected_before_save():
    class FakeDownload:
        suggested_filename = "OperaSetup.exe"
        def save_as(self, path):
            raise AssertionError("executable must not be written")

    try:
        epub_fetcher._save_download(FakeDownload(), ".")
    except epub_fetcher.FetchError as exc:
        check("OperaSetup.exe" in str(exc), str(exc))
    else:
        raise AssertionError("executable download was accepted")


def test_safe_filename():
    check(epub_fetcher.safe_filename("../../etc/passwd") == "passwd.epub", "traversal stripped")
    check(epub_fetcher.safe_filename('Vol: 1?*.epub') == "Vol_ 1_.epub", "invalid characters")
    check(epub_fetcher.safe_filename("") == "novel.epub", "fallback")
    check(epub_fetcher.safe_filename("book.pdf") == "book.pdf", "suffix kept")
    check(epub_fetcher.safe_filename("book") == "book.epub", "suffix added")


def test_flow_heuristics():
    check(epub_fetcher.is_challenge_text("Just a moment..."), "cloudflare marker")
    check(epub_fetcher.is_challenge_text("Verify you are human"), "turnstile marker")
    check(not epub_fetcher.is_challenge_text("Volume 23 download"), "normal page")
    check(
        epub_fetcher.candidate_click_labels(["Download", "Get Link", "Download"]) == ["get link", "download"],
        "priority + dedupe",
    )
    check(epub_fetcher.looks_like_file_url("https://cdn.example/a/b.epub?sig=1"), "epub link")
    check(not epub_fetcher.looks_like_file_url("https://zshort.link/abc"), "shortener link")


def test_challenge_visibility_after_manual_click():
    class FakePage:
        def __init__(self, text, html, explode=False):
            self.text = text
            self.html = html
            self.explode = explode
        def inner_text(self, selector):
            if self.explode:
                raise RuntimeError("page closed")
            return self.text
        def content(self):
            return self.html

    check(not epub_fetcher._challenge_visible(FakePage("Continue to download", "<html></html>")), "cleared page")
    check(epub_fetcher._challenge_visible(FakePage("Verify you are human", "<html></html>")), "text still challenged")
    check(
        epub_fetcher._challenge_visible(FakePage("Continue", '<iframe src="https://challenges.cloudflare.com/x"></iframe>')),
        "iframe still challenged",
    )
    check(epub_fetcher._challenge_visible(FakePage("", "", explode=True)), "unreadable treated as challenged")


def test_flaresolverr_cookies():
    solution = {
        "cookies": [
            {"name": "cf_clearance", "value": "x", "domain": ".example.com", "path": "/", "secure": True, "httpOnly": True},
            {"name": "session", "value": "y"},
            {"value": "no-name"},
        ]
    }
    cookies = epub_fetcher.FlareSolverrClient.cookies_for_context(solution)
    check(len(cookies) == 2, cookies)
    check(cookies[0]["domain"] == ".example.com" and cookies[0]["secure"] is True, cookies[0])
    check(cookies[1]["path"] == "/", cookies[1])


def test_file_url_suffixes():
    check(epub_fetcher.safe_filename("Novel Vol 1.pdf").endswith(".pdf"), "pdf name")
    check(epub_fetcher.looks_like_file_url("https://x/y.PDF"), "case-insensitive suffix")


def test_flaresolverr_request_shape():
    original = epub_fetcher.urllib.request.urlopen
    captured = {}

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload
        def read(self):
            return self.payload
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse(json.dumps({
            "status": "ok",
            "solution": {"url": "https://cleared.example/page", "cookies": []},
        }).encode("utf-8"))

    epub_fetcher.urllib.request.urlopen = fake_urlopen
    try:
        solution = epub_fetcher.FlareSolverrClient("http://solver:8191", timeout=9).request_get(
            "https://gate.example/x",
            cookies=[{"name": "tp", "value": "zSvE6kPM"}, {"name": "", "value": "ignored"}],
        )
        check(solution["url"] == "https://cleared.example/page", solution)
        check(captured["url"] == "http://solver:8191/v1", captured)
        check(captured["body"]["cmd"] == "request.get", captured)
        check(captured["body"]["url"] == "https://gate.example/x", captured)
        check(captured["body"]["waitInSeconds"] == 2, captured)
        check(captured["body"]["cookies"] == [{"name": "tp", "value": "zSvE6kPM"}], captured)
        check("tabs_till_verify" not in captured["body"], captured)
        check(captured["timeout"] == 9, captured)
    finally:
        epub_fetcher.urllib.request.urlopen = original


def test_tabs_env_is_forwarded():
    previous = os.environ.get("EPUB_FETCHER_FLARESOLVERR_TABS")
    original = epub_fetcher.urllib.request.urlopen
    captured = {}

    class FakeResponse:
        def read(self):
            return json.dumps({"status": "ok", "solution": {"url": "https://cleared.example/page"}}).encode()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=0):
        captured["body"] = json.loads(request.data.decode())
        return FakeResponse()

    os.environ["EPUB_FETCHER_FLARESOLVERR_TABS"] = "1"
    epub_fetcher.urllib.request.urlopen = fake_urlopen
    try:
        epub_fetcher.FlareSolverrClient("http://solver:8191").request_get("https://gate.example/x")
        check(captured["body"]["tabs_till_verify"] == 1, captured)
    finally:
        epub_fetcher.urllib.request.urlopen = original
        if previous is None:
            os.environ.pop("EPUB_FETCHER_FLARESOLVERR_TABS", None)
        else:
            os.environ["EPUB_FETCHER_FLARESOLVERR_TABS"] = previous


def test_flaresolverr_session_warmup_payloads():
    original = epub_fetcher.urllib.request.urlopen
    captured = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload
        def read(self):
            return self.payload
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=0):
        body = json.loads(request.data.decode("utf-8"))
        captured.append(body)
        if body["cmd"] == "sessions.create":
            return FakeResponse(b'{"status":"ok","session":"sess-1"}')
        if body["cmd"] == "sessions.destroy":
            return FakeResponse(b'{"status":"ok"}')
        return FakeResponse(b'{"status":"ok","solution":{"url":"https://gate.example/x"}}')

    previous_tabs = os.environ.get("EPUB_FETCHER_FLARESOLVERR_TABS")
    os.environ["EPUB_FETCHER_FLARESOLVERR_TABS"] = "1"
    epub_fetcher.urllib.request.urlopen = fake_urlopen
    try:
        client = epub_fetcher.FlareSolverrClient("http://solver:8191")
        session = client.create_session()
        check(session == "sess-1", session)
        client.request_get(
            "https://gate.example/x",
            cookies=[{"name": "tp", "value": "abc"}],
            session=session,
            include_tabs=False,
        )
        client.request_get("https://gate.example/x", session=session)
        client.destroy_session(session)
    finally:
        epub_fetcher.urllib.request.urlopen = original
        if previous_tabs is None:
            os.environ.pop("EPUB_FETCHER_FLARESOLVERR_TABS", None)
        else:
            os.environ["EPUB_FETCHER_FLARESOLVERR_TABS"] = previous_tabs
    check(captured[0]["cmd"] == "sessions.create", captured)
    check(captured[1]["session"] == "sess-1" and "tabs_till_verify" not in captured[1], captured[1])
    check(captured[1]["cookies"] == [{"name": "tp", "value": "abc"}], captured[1])
    check(captured[2]["session"] == "sess-1" and captured[2]["tabs_till_verify"] == 1, captured[2])
    check(captured[3]["cmd"] == "sessions.destroy", captured)


def main():
    test_safe_filename()
    test_book_archive_signatures()
    test_executable_download_rejected_before_save()
    test_flow_heuristics()
    test_challenge_visibility_after_manual_click()
    test_flaresolverr_cookies()
    test_file_url_suffixes()
    test_flaresolverr_request_shape()
    test_tabs_env_is_forwarded()
    test_flaresolverr_session_warmup_payloads()
    print("epub fetcher helper suite PASSED")


if __name__ == "__main__":
    main()
